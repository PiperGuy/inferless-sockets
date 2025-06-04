#!/usr/bin/env bash
set -euo pipefail
STREAM="inferless-logs-stream"
PAGE=100           # records per GetRecords call
SLEEP=0.3          # pause to stay under shard/sec limits

shards=$(aws kinesis list-shards --stream-name "$STREAM" \
          --query 'Shards[].ShardId' --output text)

for SHARD in $shards; do
  echo "=== Shard $SHARD ==="

  ITER=$(aws kinesis get-shard-iterator \
           --stream-name "$STREAM" \
           --shard-id     "$SHARD" \
           --shard-iterator-type TRIM_HORIZON \
           --query ShardIterator --output text)

  while [[ "$ITER" != "null" && -n "$ITER" ]]; do
    RESP=$(aws kinesis get-records --shard-iterator "$ITER" --limit $PAGE)
    ITER=$(echo "$RESP" | jq -r '.NextShardIterator')

    echo "$RESP" | jq -r '.Records[].Data' | base64 --decode

    # stop if no more records to avoid busy-looping at the tip
    [[ $(echo "$RESP" | jq '.Records | length') -eq 0 ]] && break
    sleep $SLEEP
  done
done
