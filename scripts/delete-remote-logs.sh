aws kinesis delete-stream \
  --stream-name inferless-logs-stream \
  --enforce-consumer-deletion

# 2) Wait until it's gone
aws kinesis wait stream-not-exists \
  --stream-name inferless-logs-stream

# 3) Create a fresh, empty stream with one shard
aws kinesis create-stream \
  --stream-name inferless-logs-stream \
  --shard-count 1

# 4) Wait until it's active
aws kinesis wait stream-exists \
  --stream-name inferless-logs-stream

# 5) Enable the event source mapping for the $LATEST version
aws lambda update-event-source-mapping \
  --uuid bd30f6b0-bf58-4d46-9caa-6d5d85d1a401 \
  --enabled