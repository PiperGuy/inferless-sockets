import boto3
import json
import os
import logging

# Initialize clients and config
ddb = boto3.client("dynamodb")
lambdacli = boto3.client("lambda")
TABLE = os.environ["TABLE"]
CONSUMER_ARN = os.environ.get("CONSUMER_ARN")

# Logger setup
log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    route = event["requestContext"]["routeKey"]
    cid = event["requestContext"]["connectionId"]
    pk = {"PK": {"S": f"CONN#{cid}"}}

    # 1) $connect: register the new connection
    if route == "$connect":
        user = event["requestContext"]["authorizer"]["userId"]
        ddb.put_item(TableName=TABLE, Item={**pk, "userId": {"S": user}})
        log.info("Connected: %s (user %s)", cid, user)
        return {"statusCode": 200, "body": "connected"}

    # 2) streamLogs: record the desired modelId and notify consumer for back-fill
    if route == "streamLogs":
        body = json.loads(event.get("body", "{}"))
        model = body.get("modelId")
        if not model:
            return {"statusCode": 400, "body": "modelId required"}

        ddb.update_item(
            TableName=TABLE,
            Key=pk,
            UpdateExpression="SET modelId = :m",
            ExpressionAttributeValues={":m": {"S": model}},
        )
        log.info("Subscribed: %s → %s", cid, model)

        # Notify the Consumer Lambda for back-fill
        if CONSUMER_ARN:
            payload = {
                "type": "control",
                "action": "set",
                "connectionId": cid,
                "modelId": model,
            }
            lambdacli.invoke(
                FunctionName=CONSUMER_ARN,
                InvocationType="Event",
                Payload=json.dumps(payload).encode(),
            )

        return {"statusCode": 200, "body": json.dumps({"ack": "OK", "modelId": model})}

    # 3) stopStream: clear the subscription and notify consumer
    if route == "stopStream":
        ddb.update_item(TableName=TABLE, Key=pk, UpdateExpression="REMOVE modelId")
        log.info("Unsubscribed: %s", cid)

        if CONSUMER_ARN:
            payload = {"type": "control", "action": "drop", "connectionId": cid}
            lambdacli.invoke(
                FunctionName=CONSUMER_ARN,
                InvocationType="Event",
                Payload=json.dumps(payload).encode(),
            )

        return {"statusCode": 200, "body": json.dumps({"ack": "stopped"})}

    # 4) $disconnect: clean up the connection entry and notify consumer
    if route == "$disconnect":
        ddb.delete_item(TableName=TABLE, Key=pk)
        log.info("Disconnected: %s", cid)

        if CONSUMER_ARN:
            payload = {"type": "control", "action": "drop", "connectionId": cid}
            lambdacli.invoke(
                FunctionName=CONSUMER_ARN,
                InvocationType="Event",
                Payload=json.dumps(payload).encode(),
            )

        return {"statusCode": 200, "body": "disconnected"}

    # Unknown route
    log.warning("Unknown route: %s", route)
    return {"statusCode": 400, "body": "unknown route"}
