import boto3
import json
import os
import base64
import logging
from time import sleep

# AWS clients
ddb = boto3.client("dynamodb")
kinesis = boto3.client("kinesis")
mgmt = boto3.client(
    "apigatewaymanagementapi", endpoint_url=os.environ["WS_CALLBACK_URL"]
)

# Configuration
STREAM = os.environ["STREAM"]
TABLE = os.environ["TABLE"]
GSI = "ModelIndex"

# In-memory subscription map: connectionId -> modelId
MODEL_FILTER: dict[str, str] = {}

# Logging setup
log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, _ctx):
    # Distinguish control (async invokes) vs Kinesis batches
    if event.get("type") == "control":
        _process_control(event)
    elif "Records" in event:
        _process_kinesis(event)
    else:
        log.warning("Unknown event payload: %s", event)


def _process_control(payload: dict):
    cid = payload["connectionId"]
    action = payload["action"]

    if action == "set":
        model = payload["modelId"]
        MODEL_FILTER[cid] = model
        log.info("Subscribed %s → %s", cid, model)

        # Back-fill existing logs from TRIM_HORIZON
        shards = kinesis.describe_stream(StreamName=STREAM)["StreamDescription"][
            "Shards"
        ]
        for shard in shards:
            it = kinesis.get_shard_iterator(
                StreamName=STREAM,
                ShardId=shard["ShardId"],
                ShardIteratorType="TRIM_HORIZON",
            )["ShardIterator"]

            while it:
                resp = kinesis.get_records(ShardIterator=it, Limit=100)
                it = resp.get("NextShardIterator")
                if not resp["Records"]:
                    break

                for rec in resp["Records"]:
                    # rec['Data'] is raw bytes, not base64-encoded
                    raw = rec["Data"]
                    try:
                        payload_str = (
                            raw.decode("utf-8")
                            if isinstance(raw, (bytes, bytearray))
                            else raw
                        )
                        logdata = json.loads(payload_str)
                    except Exception as e:
                        log.warning("Skipping invalid backfill record: %s", e)
                        continue

                    if logdata.get("modelId") == model:
                        try:
                            mgmt.post_to_connection(
                                ConnectionId=cid, Data=json.dumps(logdata).encode()
                            )
                        except mgmt.exceptions.GoneException:
                            log.warning("Connection %s gone during backfill", cid)
                            MODEL_FILTER.pop(cid, None)
                            return

                # throttle to avoid Kinesis limits
                sleep(0.2)

        log.info("Back-fill complete for %s → %s", cid, model)

    elif action == "drop":
        if cid in MODEL_FILTER:
            MODEL_FILTER.pop(cid)
            log.info("Unsubscribed %s", cid)


def _process_kinesis(event):
    # Decode real-time batch
    logs = [
        json.loads(base64.b64decode(r["kinesis"]["data"])) for r in event["Records"]
    ]
    log.info("Real-time batch: %d records", len(logs))

    # Group logs by model
    by_model: dict[str, list] = {}
    for l in logs:
        mid = l.get("modelId")
        if mid:
            by_model.setdefault(mid, []).append(l)

    # Fan-out per model
    for model, batch in by_model.items():
        resp = ddb.query(
            TableName=TABLE,
            IndexName=GSI,
            KeyConditionExpression="modelId = :m",
            ExpressionAttributeValues={":m": {"S": model}},
        )
        for itm in resp.get("Items", []):
            cid = itm["PK"]["S"].split("#", 1)[1]
            try:
                mgmt.post_to_connection(
                    ConnectionId=cid, Data=json.dumps(batch).encode()
                )
                log.info("Pushed %d logs to %s", len(batch), cid)
            except mgmt.exceptions.GoneException:
                log.warning("Connection %s gone, removing record", cid)
                ddb.delete_item(TableName=TABLE, Key={"PK": itm["PK"]})
