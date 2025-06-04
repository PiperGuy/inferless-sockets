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

    # 2) streamLogs: record the desired identifierId and notify consumer for back-fill
    if route == "streamLogs":
        body = json.loads(event.get("body", "{}"))
        identifiers = body.get("identifierId")
        if not identifiers:
            return {"statusCode": 400, "body": "identifierId required"}

        # Ensure identifiers is always a list
        if not isinstance(identifiers, list):
            identifiers = [identifiers]

        # Store as a string list in DynamoDB
        identifier_items = [{"S": identifier} for identifier in identifiers]

        # Use the first identifier as the GSI key (required to be a string)
        first_identifier = identifiers[0] if identifiers else ""

        ddb.update_item(
            TableName=TABLE,
            Key=pk,
            UpdateExpression="SET identifierId = :ids, identifierIdGSI = :first_id",
            ExpressionAttributeValues={
                ":ids": {"L": identifier_items},
                ":first_id": {"S": first_identifier},
            },
        )
        log.info("Subscribed: %s → %s", cid, identifiers)

        # Notify the Consumer Lambda for back-fill
        if CONSUMER_ARN:
            payload = {
                "type": "control",
                "action": "set",
                "connectionId": cid,
                "identifierId": identifiers,
            }
            lambdacli.invoke(
                FunctionName=CONSUMER_ARN,
                InvocationType="Event",
                Payload=json.dumps(payload).encode(),
            )

        return {
            "statusCode": 200,
            "body": json.dumps({"ack": "OK", "identifierId": identifiers}),
        }

    # 3) stopStream: clear the subscription and notify consumer
    if route == "stopStream":
        ddb.update_item(
            TableName=TABLE,
            Key=pk,
            UpdateExpression="REMOVE identifierId, identifierIdGSI",
        )
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
