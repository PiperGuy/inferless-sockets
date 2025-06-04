#!/usr/bin/env bash
set -euo pipefail

# Default identifier - can be overridden with command line argument
IDENTIFIER="${1:-17338c83016a442c9266a4e1fd28b68b}"
STREAM="inferless-logs-stream"
PAGE=1000         # records per GetRecords call
SLEEP=0.3         # pause to stay under shard/sec limits

echo "Fetching logs for identifier: $IDENTIFIER"

# Function to process and filter logs
process_log() {
  local log="$1"
  # More flexible matching patterns
  if echo "$log" | grep -q "\"identifierId\":\"$IDENTIFIER\"" || 
     echo "$log" | grep -q "\"$IDENTIFIER" || 
     echo "$log" | grep -q "$IDENTIFIER" ; then
    # Just remove the identifier prefix from the log content but keep the full JSON
    local parsed=$(echo "$log" | jq '.')
    # Clean up the identifier prefix in the log field if present
    if echo "$parsed" | jq -e 'has("log")' > /dev/null; then
      parsed=$(echo "$parsed" | jq --arg id "$IDENTIFIER" '.log = (.log | gsub($id + "\\s*-*\\s*"; ""))')
    fi
    echo "$parsed"
  fi
}

shards=$(aws kinesis list-shards --stream-name "$STREAM" \
          --query 'Shards[].ShardId' --output text)

echo "======= LOGS FOR IDENTIFIER: $IDENTIFIER ======="

for SHARD in $shards; do
  echo "=== Checking Shard $SHARD ===" >&2

  ITER=$(aws kinesis get-shard-iterator \
           --stream-name "$STREAM" \
           --shard-id     "$SHARD" \
           --shard-iterator-type TRIM_HORIZON \
           --query ShardIterator --output text)

  records_found=0
  
  while [[ "$ITER" != "null" && -n "$ITER" ]]; do
    RESP=$(aws kinesis get-records --shard-iterator "$ITER" --limit $PAGE)
    ITER=$(echo "$RESP" | jq -r '.NextShardIterator')
    
    echo "Checking batch of logs..." >&2
    
    # Process each record - using a simpler approach to avoid process substitution
    echo "$RESP" | jq -r '.Records[].Data' | base64 --decode | while read -r log_entry; do
      if [[ -n "$log_entry" ]]; then
        output=$(process_log "$log_entry")
        if [[ -n "$output" ]]; then
          echo "$output"
          records_found=$((records_found + 1))
        fi
      fi
    done

    # stop if no more records to avoid busy-looping at the tip
    records_count=$(echo "$RESP" | jq '.Records | length')
    echo "Processed $records_count records from shard $SHARD (found: $records_found matching logs)" >&2
    [[ $records_count -eq 0 ]] && break
    sleep $SLEEP
  done
done

echo "======================================"
echo "Log retrieval complete for identifier: $IDENTIFIER" 