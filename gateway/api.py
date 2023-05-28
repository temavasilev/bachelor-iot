
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from typing import List
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncpg
import requests
import httpx

app = FastAPI()
# enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Change this to the frontend url
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config = json.load(open("config.json"))
host = config["postgres_setup"]["host"]
user = config["postgres_setup"]["user"]
password = config["postgres_setup"]["password"]
database = config["postgres_setup"]["database"]

# Pydantic model
class Datapoint(BaseModel):
    object_id: str
    jsonpath: str
    topic: str
    entity_id: Optional[str] = Field(None, min_length=1, max_length=255)
    attribute_name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = ''
    matchDatapoint: Optional[bool] = False
  
class DatapointUpdate(BaseModel):
    entity_id: Optional[str] = Field(None, min_length=1, max_length=255)
    attribute_name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = ''
    
# Database connection settings
DATABASE_URL = f"postgres://{user}:{password}@{host}:5432/{database}"
ORION_URL = f"http://{host}:1026/v2/entities"

@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(DATABASE_URL)
    
@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()
    
async def get_connection():
    async with app.state.pool.acquire() as connection:
        yield connection
        
async def postgres_notify(channel: str, payload: str, conn: asyncpg.Connection):
    print(f"Sending notification to channel {channel} with payload {payload}")
    await conn.execute(f"NOTIFY {channel}, '{payload}'")

@app.get("/data", response_model=List[Datapoint])
async def get_datapoints(conn: asyncpg.Connection = Depends(get_connection)):
    rows = await conn.fetch("SELECT object_id, jsonpath, topic, entity_id, attribute_name, description FROM devices")
    return rows

@app.get("/data/{object_id}", response_model=Datapoint)
async def get_datapoint(object_id: str, conn: asyncpg.Connection = Depends(get_connection)):
    row = await conn.fetchrow(
        """SELECT * FROM devices WHERE object_id=$1""",
        object_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found!")
    return row

@app.post("/data", response_model=Datapoint, status_code=201)
async def add_datapoint(datapoint: Datapoint, conn: asyncpg.Connection = Depends(get_connection)):
    if datapoint.matchDatapoint and (datapoint.entity_id is None or datapoint.attribute_name is None):
        raise HTTPException(status_code=400, detail="entity_id and attribute_name must be set if Match Datapoint is enabled!")
    try:
        # check if there is already a device subscribed to the same topic
        # if so, the gateway will not subscribe to the topic again
        subscribe = await conn.fetchrow(
            """SELECT object_id FROM devices WHERE topic=$1 AND object_id!=$2""",
            datapoint.topic, datapoint.object_id
        )
        await conn.execute(
            """INSERT INTO devices (object_id, jsonpath, topic, entity_id, attribute_name, description) 
            VALUES ($1, $2, $3, $4, $5, $6)""",
            datapoint.object_id, datapoint.jsonpath, datapoint.topic,
            datapoint.entity_id, datapoint.attribute_name, datapoint.description
        )
        await postgres_notify("add_datapoint", json.dumps({"object_id": datapoint.object_id,
                                                            "jsonpath": datapoint.jsonpath,
                                                            "topic": datapoint.topic,
                                                            "subscribe": subscribe is None}), conn)
        return {**datapoint.dict(), "subscribe": subscribe is None}
    
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(status_code=400, detail="Device already exists!")

@app.put("/data/{object_id}", response_model=DatapointUpdate)
async def update_datapoint(object_id: str, datapoint: DatapointUpdate, conn: asyncpg.Connection = Depends(get_connection)):
    await conn.execute(
        """UPDATE devices SET entity_id=$1, attribute_name=$2, description=$3 WHERE object_id=$4""",
        datapoint.entity_id, datapoint.attribute_name, datapoint.description, object_id
    )
    return {**datapoint.dict()}

@app.delete("/data/{object_id}", status_code=204)
async def delete_datapoint(object_id: str, conn: asyncpg.Connection = Depends(get_connection)):
    topic, jsonpath = await conn.fetchrow(
        """SELECT topic, jsonpath FROM devices WHERE object_id=$1""",
        object_id
    )
    # check if the topic is the last subscriber
    # if so, the gateway will unsubscribe from the topic
    unsubscribe = await conn.fetchrow(
        """SELECT object_id FROM devices WHERE topic=$1 AND object_id!=$2""",
        topic, object_id
    )
    await conn.execute(
        """DELETE FROM devices WHERE object_id=$1""",
        object_id
    )
    
    await postgres_notify("remove_datapoint", json.dumps({"object_id": object_id,
                                                         "topic": topic,
                                                         "jsonpath": jsonpath,
                                                         "unsubscribe": unsubscribe is None}), conn)
    return None

@app.get("/data/{object_id}/status", response_model=bool)
async def get_match_status(object_id: str, conn: asyncpg.Connection = Depends(get_connection)):
    row = await conn.fetchrow(
        """SELECT entity_id, attribute_name FROM devices WHERE object_id=$1""",
        object_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found!")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{ORION_URL}/{row['entity_id']}/attrs/{row['attribute_name']}")
        return response.status_code == 200

