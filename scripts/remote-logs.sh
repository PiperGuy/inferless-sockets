#!/usr/bin/env bash
set -euo pipefail

STREAM="inferless-logs-stream"
PAGE_SIZE=100   # number of records per GetRecords call
SLEEP=0.5       # seconds between calls to avoid throttling

# 1) Get all shard IDs
shards=$(aws kinesis list-shards \
    --stream-name "$STREAM" \
    --query 'Shards[].ShardId' \
    --output text \
    | tr '\t' '\n')

for shard in $shards; do
  echo "=== Reading shard $shard ==="

  # 2) Start at TRIM_HORIZON (oldest available record)
  iterator=$(aws kinesis get-shard-iterator \
      --stream-name "$STREAM" \
      --shard-id "$shard" \
      --shard-iterator-type TRIM_HORIZON \
      --query "ShardIterator" --output text)

  # 3) Loop until no iterator or no records
  while [[ -n "$iterator" && "$iterator" != "null" ]]; do
    # Fetch a page of records
    resp=$(aws kinesis get-records \
      --shard-iterator "$iterator" \
      --limit $PAGE_SIZE \
      --output json)

    # Print decoded JSON for each record
    echo "$resp" | jq -r '.Records[].Data' \
      | base64 --decode \
      | jq .

    # Advance to the next position
    iterator=$(echo "$resp" | jq -r '.NextShardIterator')

    # If you want to stop when you hit the tip, break when no Records:
    [[ $(echo "$resp" | jq '.Records | length') -eq 0 ]] && break

    sleep $SLEEP
  done
done
