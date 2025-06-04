#!/usr/bin/env bash
set -euo pipefail

STREAM="inferless-logs-stream"
LIMIT=50  # Number of most recent logs to display
SHARD="shardId-000000000000"  # Default to first shard

echo "Showing $LIMIT most recent logs from Kinesis stream: $STREAM"

# Get iterator for the latest position (LATEST)
ITER=$(aws kinesis get-shard-iterator \
         --stream-name "$STREAM" \
         --shard-id     "$SHARD" \
         --shard-iterator-type LATEST \
         --query ShardIterator --output text)

echo "Waiting for new logs... (press Ctrl+C to stop)"
sleep 2

# First check if there are any recent logs
RESP=$(aws kinesis get-records --shard-iterator "$ITER" --limit $LIMIT)
RECORDS_COUNT=$(echo "$RESP" | jq '.Records | length')

if [[ $RECORDS_COUNT -eq 0 ]]; then
  echo "No new logs found. Showing some recent logs from history instead..."
  
  # Try TRIM_HORIZON instead to get some historical logs
  ITER=$(aws kinesis get-shard-iterator \
           --stream-name "$STREAM" \
           --shard-id     "$SHARD" \
           --shard-iterator-type TRIM_HORIZON \
           --query ShardIterator --output text)
           
  # Get a bunch of records but only display the most recent ones
  RESP=$(aws kinesis get-records --shard-iterator "$ITER" --limit 1000)
  
  # Extract just the last LIMIT records
  RECORDS=$(echo "$RESP" | jq -c ".Records[-$LIMIT:] | reverse")
else
  # Use the records we already got
  RECORDS=$(echo "$RESP" | jq -c '.Records | reverse')
fi

# Process and display the logs
echo "$RECORDS" | jq -c '.[]' | while read -r record; do
  DATA=$(echo "$record" | jq -r '.Data' | base64 --decode)
  echo "$DATA" | jq -r '.'
  echo "---------------------------------------------------"
done

echo "Done!" 