import re
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
GSI = "identifier-index"

# In-memory subscription map: connectionId -> identifierId list
IDENTIFIER_FILTER: dict[str, list[str]] = {}

# In-memory log deduplication cache: connectionId -> set of processed log hashes
LOG_DEDUP_CACHE: dict[str, set[str]] = {}
MAX_CACHE_SIZE = 1000  # Maximum number of log hashes to keep per connection

# Logging setup
log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, _ctx):
    if event.get("type") == "control":
        _process_control(event)
    elif "Records" in event:
        _process_kinesis(event)
    else:
        log.warning("Unknown event payload: %s", event)


def _decode_data(raw_data):
    """
    Decode Kinesis record Data field, handling both raw JSON bytes or base64-encoded payloads.
    """
    if isinstance(raw_data, (bytes, bytearray)):
        text = raw_data.decode("utf-8", errors="ignore")
    else:
        text = str(raw_data)
    # Try direct JSON
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try base64 then JSON
    try:
        decoded = base64.b64decode(text)
        return json.loads(decoded)
    except Exception:
        log.warning("Failed to decode record Data for backfill")
        return None


def _generate_log_hash(log_data):
    """Generate a hash for a log entry to detect duplicates."""
    if isinstance(log_data, dict):
        # Hash based on content, time, and identifier if available
        log_content = log_data.get("log", "")
        timestamp = log_data.get("time", "")
        identifier = log_data.get("identifierId", "")
        hash_input = f"{log_content}:{timestamp}:{identifier}"
        return hash(hash_input)
    return hash(str(log_data))


def _is_duplicate_log(cid, log_data):
    """Check if this log has already been sent to this connection."""
    # Initialize dedup cache for this connection if needed
    if cid not in LOG_DEDUP_CACHE:
        LOG_DEDUP_CACHE[cid] = set()

    # Generate a hash for this log
    log_hash = _generate_log_hash(log_data)

    # Check if we've seen this log before
    if log_hash in LOG_DEDUP_CACHE[cid]:
        return True

    # Add to cache for future checks
    LOG_DEDUP_CACHE[cid].add(log_hash)

    # Limit cache size to prevent memory growth
    if len(LOG_DEDUP_CACHE[cid]) > MAX_CACHE_SIZE:
        # Remove oldest entries (approximate by converting to list, but works for our purpose)
        cache_list = list(LOG_DEDUP_CACHE[cid])
        LOG_DEDUP_CACHE[cid] = set(cache_list[-MAX_CACHE_SIZE:])

    return False


def _process_control(payload: dict):
    cid = payload["connectionId"]
    action = payload["action"]

    if action == "set":
        identifiers = payload["identifierId"]
        # Ensure identifiers is always a list
        if not isinstance(identifiers, list):
            identifiers = [identifiers]

        # Log the exact format of identifiers received
        log.info("RECEIVED IDENTIFIERS: %s (type: %s)", identifiers, type(identifiers))

        # Skip if empty list
        if not identifiers:
            log.warning("Empty identifierId list for connection %s, skipping", cid)
            return

        IDENTIFIER_FILTER[cid] = identifiers
        log.info("Subscribed %s → %s", cid, identifiers)

        # Clear deduplication cache for this connection
        LOG_DEDUP_CACHE.pop(cid, {})

        # Back-fill: read all shards from TRIM_HORIZON to now
        try:
            shards = kinesis.describe_stream(StreamName=STREAM)["StreamDescription"][
                "Shards"
            ]

            log.info(
                "Starting backfill for identifiers: %s (Stream: %s)",
                identifiers,
                STREAM,
            )
            log.info("Found %d shards in stream", len(shards))
            backfill_count = 0
            processed_count = 0

            for shard_idx, shard in enumerate(shards):
                log.info(
                    "Processing shard %d/%d: %s",
                    shard_idx + 1,
                    len(shards),
                    shard["ShardId"],
                )
                try:
                    it = kinesis.get_shard_iterator(
                        StreamName=STREAM,
                        ShardId=shard["ShardId"],
                        ShardIteratorType="TRIM_HORIZON",
                    )["ShardIterator"]

                    iteration = 0
                    shard_records = 0

                    while it:
                        iteration += 1
                        resp = kinesis.get_records(ShardIterator=it, Limit=1000)
                        it = resp.get("NextShardIterator")
                        records = resp.get("Records", [])
                        shard_records += len(records)

                        if iteration % 10 == 0:
                            log.info(
                                "Processed %d iterations, %d records so far in shard %s",
                                iteration,
                                shard_records,
                                shard["ShardId"],
                            )

                        if not records:
                            log.info("No more records in shard %s", shard["ShardId"])
                            break

                        for rec in records:
                            processed_count += 1
                            try:
                                logdata = _decode_data(rec.get("Data"))
                                if not logdata:
                                    log.debug("Skipping record - couldn't decode data")
                                    continue

                                # Print a sample of what we're seeing
                                if processed_count <= 5:
                                    log.info(
                                        "SAMPLE RECORD: %s", json.dumps(logdata)[:200]
                                    )

                                # Check if the record's identifierId is in the subscription list
                                record_id = logdata.get("identifierId")

                                # Debug every record for troubleshooting
                                log.info(
                                    "Checking record with ID: %s against subscriptions: %s",
                                    record_id,
                                    identifiers,
                                )

                                # Debug check for first few records
                                if processed_count <= 20:
                                    log.info(
                                        "Record %d has identifierId: %s",
                                        processed_count,
                                        record_id,
                                    )

                                if not record_id:
                                    log.debug("Skipping record with no identifierId")
                                    continue

                                # Handle both string and list cases
                                record_ids = (
                                    record_id
                                    if isinstance(record_id, list)
                                    else [record_id]
                                )

                                # For each ID in our subscription, check if it matches any ID in the record
                                match_found = False
                                for sub_id in identifiers:
                                    for rid in record_ids:
                                        # Try multiple matching approaches
                                        if str(sub_id).strip() == str(rid).strip():
                                            match_found = True
                                            log.info(
                                                "MATCH FOUND (exact): %s == %s",
                                                sub_id,
                                                rid,
                                            )
                                            break
                                        # Try case-insensitive match
                                        elif (
                                            str(sub_id).strip().lower()
                                            == str(rid).strip().lower()
                                        ):
                                            match_found = True
                                            log.info(
                                                "MATCH FOUND (case-insensitive): %s == %s",
                                                sub_id,
                                                rid,
                                            )
                                            break
                                    if match_found:
                                        break

                                if match_found:
                                    try:
                                        log.info("Sending matched record to %s", cid)
                                        # Process log through extract_log_item before sending
                                        processed_log = extract_log_item(logdata)

                                        # Skip if this is a duplicate log
                                        if _is_duplicate_log(cid, processed_log):
                                            log.info(
                                                "Skipping duplicate log for %s", cid
                                            )
                                            continue

                                        mgmt.post_to_connection(
                                            ConnectionId=cid,
                                            Data=json.dumps(processed_log).encode(),
                                        )
                                        backfill_count += 1
                                    except mgmt.exceptions.GoneException:
                                        log.warning(
                                            "Connection %s gone during backfill", cid
                                        )
                                        IDENTIFIER_FILTER.pop(cid, None)
                                        return
                                    except Exception as e:
                                        log.error(
                                            "Error sending backfill to %s: %s",
                                            cid,
                                            str(e),
                                        )
                            except Exception as e:
                                log.error("Error processing record: %s", str(e))

                        # throttle to avoid Kinesis limits
                        sleep(0.1)
                except Exception as e:
                    log.error("Error processing shard %s: %s", shard["ShardId"], str(e))

            log.info(
                "Back-fill complete for %s → %s, processed %d records, sent %d matching records",
                cid,
                identifiers,
                processed_count,
                backfill_count,
            )
        except Exception as e:
            log.error("Backfill error for %s: %s", cid, str(e))

    elif action == "drop":
        if cid in IDENTIFIER_FILTER:
            IDENTIFIER_FILTER.pop(cid, None)
            # Also clear deduplication cache
            LOG_DEDUP_CACHE.pop(cid, None)
            log.info("Unsubscribed %s", cid)


def _process_kinesis(event):
    # Decode real-time batch
    logs = []
    for r in event.get("Records", []):
        try:
            data = base64.b64decode(r["kinesis"]["data"])
            logs.append(json.loads(data))
        except Exception:
            log.warning("Skipping invalid realtime record")
    log.info("Real-time batch: %d records", len(logs))

    # Group logs by identifier
    by_identifier: dict[str, list] = {}
    for l in logs:
        record_id = l.get("identifierId")
        if not record_id:
            continue

        # Handle both string and list identifiers
        if isinstance(record_id, str):
            record_ids = [record_id]
        elif isinstance(record_id, list):
            record_ids = record_id
        else:
            continue

        for id in record_ids:
            if id:  # Skip empty identifiers
                by_identifier.setdefault(id, []).append(l)

    # Process for each identifier that has logs
    for identifier, identifier_logs in by_identifier.items():
        if not identifier or not identifier_logs:
            continue

        try:
            # First try a direct query on the GSI for exact matches on the first identifier
            resp = ddb.query(
                TableName=TABLE,
                IndexName=GSI,
                KeyConditionExpression="identifierIdGSI = :id",
                ExpressionAttributeValues={":id": {"S": identifier}},
            )

            # Get additional matches through filtering for identifiers in the list
            scan_resp = ddb.scan(
                TableName=TABLE,
                FilterExpression="contains(identifierId, :id) AND attribute_not_exists(identifierIdGSI)",
                ExpressionAttributeValues={":id": {"S": identifier}},
            )

            # Combine results
            items = resp.get("Items", []) + scan_resp.get("Items", [])

            # For each connection, send the logs
            for item in items:
                cid = item["PK"]["S"].split("#", 1)[1]
                try:
                    # Process logs through extract_log_item before sending
                    processed_logs = []
                    for log_item in identifier_logs:
                        processed_log = extract_log_item(log_item)

                        # Skip empty logs
                        if not processed_log.get("log", ""):
                            continue

                        # Skip duplicates
                        if not _is_duplicate_log(cid, processed_log):
                            processed_logs.append(processed_log)
                        else:
                            log.info("Skipping duplicate real-time log for %s", cid)

                    if processed_logs:
                        mgmt.post_to_connection(
                            ConnectionId=cid, Data=json.dumps(processed_logs).encode()
                        )
                        log.info(
                            "Pushed %d logs to %s for identifier %s",
                            len(processed_logs),
                            cid,
                            identifier,
                        )
                except mgmt.exceptions.GoneException:
                    log.warning("Connection %s gone, removing record", cid)
                    try:
                        ddb.delete_item(
                            TableName=TABLE, Key={"PK": {"S": f"CONN#{cid}"}}
                        )
                        # Also clean up deduplication cache
                        LOG_DEDUP_CACHE.pop(cid, None)
                    except Exception as e:
                        log.error(
                            "Failed to delete gone connection %s: %s", cid, str(e)
                        )
                except Exception as e:
                    log.error("Error sending logs to %s: %s", cid, str(e))
        except Exception as e:
            log.error("Error processing identifier %s: %s", identifier, str(e))


log_filter_string = [
    "By pulling and using the container, you accept the terms and conditions of this license:"
]


def extract_log_item(log_item, type="BUILD", ignore=False):
    try:
        # Make a copy of the entire log item to preserve all fields
        temp_log_data = log_item.copy() if isinstance(log_item, dict) else {}

        if isinstance(log_item, str):
            temp = json.loads(log_item)
        elif isinstance(log_item, dict):
            temp = log_item

        # Debug input
        log.info("Processing log item: %s", json.dumps(temp)[:200])

        # Ensure time field exists
        if "time" in temp:
            temp_log_data["time"] = temp["time"]
        elif "@timestamp" in temp:
            temp_log_data["time"] = temp["@timestamp"]
        else:
            # Default time if missing
            temp_log_data["time"] = "unknown"

        # Get the log content, with fallback
        log_content = temp.get("log", "")
        if not log_content and "message" in temp:
            log_content = temp.get("message", "")

        log.info("Original log content: %s", log_content[:100])

        # Ensure stream field
        if "stream" in temp:
            temp_log_data["stream"] = temp["stream"]
        else:
            temp_log_data["stream"] = "stdout"

        # Preserve identifierId if present
        if "identifierId" in temp:
            temp_log_data["identifierId"] = temp["identifierId"]

        # Process the log content
        if type == "INFERENCE":
            temp_log_data["log"] = re.sub(r"^I.*?\]", "", log_content)
        elif type == "BUILD":
            temp_log_data["log"] = re.sub(r"^\[.*?\]", "", log_content)
            temp_log_data["log"] = re.sub(r"^I.*?\]", "", temp_log_data["log"])
        else:
            temp_log_data["log"] = log_content

        # Trim any leading/trailing whitespace after timestamp removal
        if "log" in temp_log_data:
            temp_log_data["log"] = temp_log_data["log"].strip()

        if not ignore:
            pattern = re.compile(r"(?i)(nvidia|triton)")
            if re.search(pattern, temp_log_data["log"]):
                temp_log_data["log"] = ""

        # Check for specific filter strings
        if log_content in log_filter_string:
            temp_log_data["log"] = ""

        # Special identifier removal - handle this first and directly
        log.info(
            "Before identifier removal: %s",
            temp_log_data["log"][:100] if "log" in temp_log_data else "No log content",
        )

        # Get exact pattern from the log line
        if "identifierId" in temp:
            identifier = temp["identifierId"]
            if isinstance(identifier, list) and identifier:
                identifier = identifier[0]
            elif not isinstance(identifier, str):
                identifier = None
        else:
            identifier = None

        # Extract any hex identifier that might be at the start
        if "log" in temp_log_data and temp_log_data["log"]:
            # Try the exact pattern with the known identifier first
            if identifier:
                log.info("Using identifier: %s", identifier)

                # Direct string replacement - for exact match at start of string after trimming
                if temp_log_data["log"].startswith(identifier):
                    # Check if followed by " - "
                    rest = temp_log_data["log"][len(identifier) :].lstrip()
                    if rest.startswith("- "):
                        temp_log_data["log"] = rest[
                            2:
                        ].strip()  # Remove "- " and any whitespace
                        log.info("Removed identifier using exact start match")

                # Pattern for identifier anywhere in the string with possible whitespace
                pattern = r"(?:\s|^)" + re.escape(identifier) + r"\s*-\s*"
                before = temp_log_data["log"]
                temp_log_data["log"] = re.sub(
                    pattern,
                    " ",
                    temp_log_data["log"],
                    count=1,  # Add count=1 to prevent multiple replacements
                ).strip()
                if before != temp_log_data["log"]:
                    log.info("Removed with flexible pattern")

            # Generic pattern for any hex ID of any length
            pattern = r"(?:\s|^)([a-f0-9]{32})\s*-\s*"
            before = temp_log_data["log"]
            temp_log_data["log"] = re.sub(
                pattern, " ", temp_log_data["log"], count=1
            ).strip()
            if before != temp_log_data["log"]:
                log.info("Removed with generic hex pattern")

            # Extra pattern specifically for your log format from the example
            specific_pattern = r"([a-f0-9]{32})\s*-\s*"
            before = temp_log_data["log"]
            temp_log_data["log"] = re.sub(
                specific_pattern, "", temp_log_data["log"], count=1
            ).strip()
            if before != temp_log_data["log"]:
                log.info("Removed with specific pattern match")

            # If all else fails, try a brutal force approach for this specific identifier
            if identifier and identifier in temp_log_data["log"]:
                parts = temp_log_data["log"].split(identifier + " - ", 1)
                if len(parts) > 1:
                    temp_log_data["log"] = parts[1].strip()
                    log.info("Removed using string split")

            # Final cleanup to ensure any remaining dash at beginning is removed
            temp_log_data["log"] = re.sub(
                r"^[\s-]+", "", temp_log_data["log"], count=1
            ).strip()

            # Extreme fallback - if we still have a dash at the beginning, remove it
            if temp_log_data["log"].startswith("-"):
                temp_log_data["log"] = temp_log_data["log"][1:].strip()
                log.info("Removed leading dash with fallback")

            # Check for specific pattern: dash followed by whitespace at beginning
            if temp_log_data["log"].startswith("- "):
                temp_log_data["log"] = temp_log_data["log"][2:].strip()
                log.info("Removed '- ' prefix")

        log.info(
            "Final extracted log: %s",
            temp_log_data["log"][:100] if "log" in temp_log_data else "No log content",
        )
        return temp_log_data

    except Exception as e:
        log.error("Error in extract_log_item: %s", str(e))
        return log_item
