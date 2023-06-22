import asyncio
import json
import time
from random import uniform
from typing import List

import asyncio_mqtt
import httpx
from asyncio_mqtt import Client as MQTTClient
from dateutil.parser import parse
from fastapi import FastAPI
from pydantic import BaseModel
from uvicorn import Config, Server
from aiologger import Logger
from aiologger.handlers.files import AsyncFileHandler
from filip.clients.ngsi_v2 import ContextBrokerClient, IoTAClient
from collections import defaultdict
from filip.models.ngsi_v2.iot import Device
from filip.utils.cleanup import clear_context_broker, clear_iot_agent

from plots.plots import plot_message_loss, plot_percentage_loss, plot_latency
import sys

CBC = ContextBrokerClient("http://localhost:1026/")
IOTA = IoTAClient("http://localhost:4041/")
MQTT_HOSTNAME = "localhost"

MAX_CLIENTS = 1000
INITIAL_CLIENTS = 100
CLIENT_STEP = 100
CREATION_INTERVAL = 15

STAGE_COUNT = MAX_CLIENTS // CLIENT_STEP

messages_sent = [0] * STAGE_COUNT
messages_received = [0] * STAGE_COUNT
messages_per_second = [0] * STAGE_COUNT
latencies = defaultdict(list)

stage = 0

logger = Logger.with_default_handlers()
logger.add_handler(AsyncFileHandler("baseline.log"))

last_message_received = asyncio.Event()
generate_messages = asyncio.Event()

async def receive_mqtt_notification() -> None:
    """
    Receive the notification from the Orion Context Broker via MQTT and calculate the latency.
    I will make use of the DateModified metadata field to calculate the latency. This field is automatically
    added by the Orion Context Broker and contains the timestamp of the last update of the entity. We can then
    calculate the latency by subtracting the timestamp attribute of the entity from the DateModified attribute.
    """
    async with asyncio_mqtt.Client("localhost") as client:
        await client.subscribe("test/timestamp")
        async with client.messages() as messages:
            async for message in messages:
                print("Received message")
                messages_received[stage] += 1
                last_message_received.set()
                times = time.time()
                payload = json.loads(message.payload)
                print(payload)
                payload_timestamp = float(payload["data"][0]["humidity"]["value"])
                date_modified = parse(
                    payload["data"][0]["humidity"]["metadata"]["dateModified"]["value"]
                ).timestamp()
                print(f"Payload timestamp: {payload_timestamp}")
                print(f"Date modified: {date_modified}")
                print(f"Time: {times}")
                latency = (times - payload_timestamp) * 1000
                latencies[stage].append(latency)
                print(f"Latency: {latency:.3f} ms (MQTT)")

async def generate_random_string(length: int) -> str:
    """
    Generate a random string of the specified length. This is used to generate random attribute names
    that are not already in use by the Orion Context Broker as we need to see whether the matching works properly.
    This is a very naive implementation, but it is sufficient for our purposes and makes use of the ascii table.
    At the end of the day, we just need to generate an 'attribute name' that is not registered in the Context Broker entity.
    """
    return "".join([chr(int(uniform(97, 122))) for _ in range(length)])


async def generate_payload() -> str:
    """
    Generate a random payload for the test/latency topic. The payload needs to contain a 'real' attribute (the one that exists in the Context Broker)
    and a 'fake' attribute (generated by generate_random_string) to test whether the matching works properly. The 'real' attribute will be picked at random
    from the list of attributes that are already in use by the Context Broker. Also, we need to add a timestamp to the payload so that we can calculate the latency.
    """
    attributes = ["temperature", "humidity", "pressure", "co2", "timestamp"]
    real_attribute = attributes[int(uniform(0, len(attributes)))]
    fake_attribute = await generate_random_string(10)
    messages_sent[stage] += 1
    return json.dumps(
        {fake_attribute: {
            "value": round(uniform(0, 100), 2), 
        }, 
        real_attribute: {
            "value": time.time()}
        }
    )

async def generate_client() -> None:
    """
    Generate a client and publish a payload to the test/latency topic every second.
    The client continuously publishes a payload every second until it is cancelled. Each payload consists of
    a real and a fake attribute and a timestamp. The real attribute is used to test whether the matching works
    properly, while the timestamp is used to calculate the latency.
    """
    try:
        async with MQTTClient(MQTT_HOSTNAME) as client:
            await client.connect()
            while True:
                await generate_messages.wait()
                try:
                    CBC.update_existing_entity_attributes(
                        "TestFacility",
                        "Room",
                        json.loads(await generate_payload()),
                    )
                except Exception as e:
                    pass
                #await client.publish("test/latency", await generate_payload())
                await asyncio.sleep(1)
    except Exception as e:
        print(f"An error occurred: {e}")


async def generate_clients(
        initial_clients: int, client_step: int, creation_interval: int
) -> None:
    """
    Generate a number of clients and publish a payload to the test/latency topic every second (potentially one could extend this to publish to different topics as well).
    The number of clients is increased by client_step every creation_interval seconds. This is done to simulate a real-world scenario where the multiple clients
    try to publish data to the broker at the same time. The client_step and creation_interval parameters are used to control the number of clients and the interval
    between the creation of new clients respectively.

    Args:
        initial_clients (int): The number of clients to start with.
        client_step (int): The number of clients to add every creation_interval seconds.
        creation_interval (int): The interval between the creation of new clients.
    """
    tasks = []
    logger = Logger.with_default_handlers(name="benchmark")
    logger.add_handler(AsyncFileHandler("benchmark.log", mode="a"))
    try:
        global stage
        for _ in range(MAX_CLIENTS // client_step):
            print(f"Stage {stage}")
            generate_messages.set()
            messages_per_second[stage] = client_step * (stage + 1)
            new_tasks = [
                asyncio.create_task(generate_client()) for _ in range(client_step)
            ]
            tasks.extend(new_tasks)
            await asyncio.sleep(creation_interval)
            generate_messages.clear()
            await wait_for_last_stage_message(10)
            stage += 1
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Cancel all tasks
        for task in tasks:
            task.cancel()
        # Wait for all tasks to complete their cancellation
        await asyncio.gather(*tasks, return_exceptions=True)
        plot_message_loss(messages_sent, messages_received, messages_per_second, baseline=True)
        plot_percentage_loss(messages_sent, messages_received, messages_per_second, baseline=True)
        plot_latency(messages_per_second, latencies, baseline=True)


async def wait_for_last_stage_message(wait_time: int = 5):
    """
    Wait for the last message to be received during each stage. 
    If no message is received in the last wait_time seconds, it returns.
    """
    while True:
        try:
            await asyncio.wait_for(last_message_received.wait(), wait_time)
            last_message_received.clear()
        except asyncio.TimeoutError:
            print(f"No messages received in the last {wait_time} seconds. Moving to the next stage...")
            break


async def main():
    try:
        test_clients = asyncio.create_task(
            generate_clients(INITIAL_CLIENTS, CLIENT_STEP, CREATION_INTERVAL)
        )
        sub_listen = asyncio.create_task(receive_mqtt_notification())
        await asyncio.gather(test_clients, sub_listen)
    except KeyboardInterrupt:
        print("Received exit signal. Shutting down...")
        for task in asyncio.all_tasks():
            task.cancel()
        print("Shutdown complete.")
        sys.exit(0)

    
if __name__ == "__main__":
    asyncio.run(main())