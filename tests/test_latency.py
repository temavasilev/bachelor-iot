import time
import paho.mqtt.client as mqtt
import matplotlib.pyplot as plt
import json
import numpy as np
from scipy.interpolate import make_interp_spline

config = json.load(open("config.json"))
mqtt_broker_address = config["connection_settings"]["server_ip"]

latencies = []
msg_per_sec = []

def on_connect(client, userdata, flags, rc):
    client.subscribe("test/latency")

def on_message(client, userdata, msg):
    latency = time.time() - float(msg.payload.decode('utf-8'))
    latencies.append(latency * 1000)
    msg_per_sec.append(current_msg_rate)

client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message
client.connect(mqtt_broker_address, 1883, 60)
client.loop_start()

num_messages = 100
# num_iterations = 10
mps_max = 500  # 1000 message per second
# num_iterations = mps_max / 10

# TODO need to ensure Message per second. Now it is not right
for freq in range(0, int(mps_max), 10):
    current_msg_rate = freq + 1
    for _ in range(current_msg_rate):
        sleep_interval = 1 / current_msg_rate
        time.sleep(sleep_interval)
        # TODO should use multi thread, after every time_interval start a new thread
        client.publish("test/latency", time.time())
    # TODO wait until this frequncy finish

client.loop_stop()

# Calculate average latency for each frequency step
average_latencies = [np.mean(latencies[i:i+num_messages]) for i in range(0, len(latencies), num_messages)]

# Smooth curve using spline interpolation
x = np.array(msg_per_sec[::num_messages])
y = np.array(average_latencies)
x_smooth = np.linspace(x.min(), x.max(), 300)
spl = make_interp_spline(x, y, k=3)
y_smooth = spl(x_smooth)

# Plot latency graph
plt.figure()
plt.scatter(msg_per_sec, latencies, color="red", marker=".", alpha=0.5, label="Raw Latency Data")
plt.scatter(msg_per_sec[::num_messages], average_latencies, marker='o', label="Average Latency")
plt.plot(x_smooth, y_smooth, label="Smoothed Average Latency")
plt.xlabel('Messages per Second')
plt.ylabel('Latency (ms)')
plt.title('Latency vs Messages per Second')
plt.legend()
plt.grid(True)
plt.show()