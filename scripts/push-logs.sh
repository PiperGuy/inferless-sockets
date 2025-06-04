#!/bin/bash

# Define possible messages
messages=(
  "runtime warn: OOM risk detected in container 8b3f" 
  "runtime error: connection timeout after 30s retry" 
  "runtime info: scaling up resources to 4 vCPUs" 
  "runtime warn: high memory usage (87% utilized)"
  "runtime info: health check passed with 23ms latency"
  "runtime warn: slow response time exceeding 500ms threshold"
  "runtime info: model loaded successfully in 1.2s"
  "runtime info: model unloaded due to inactivity"
  "runtime info: model started with optimization level 3"
  "runtime info: model stopped due to user request"
  "runtime info: model updated to version 2.4.1"
  "runtime info: model deleted from registry"
  "runtime error: CUDA out of memory in batch processor"
  "runtime warn: GPU throttling detected at 82°C"
  "runtime info: autoscaling initiated based on queue depth"
  "runtime error: failed to download weights from S3"
  "runtime warn: approaching rate limit (92/100 requests)"
  "runtime info: batch processing completed for 128 images"
  "runtime error: unexpected token in input JSON at position 347"
  "runtime warn: deprecated API call detected in request"
  "runtime info: container restarted after scheduled maintenance"
  "runtime error: failed to initialize CUDA context on worker-8"
  "runtime warn: network latency spike detected (127ms)"
  "runtime info: cache hit ratio at 78.3% for the last hour"
  "runtime error: failed to connect to database after 5 retries"
  "runtime warn: CPU utilization above 90% for 5 minutes"
  "runtime info: successful backup created at checkpoint-a7f9"
  "runtime error: invalid JWT token signature in request"
  "runtime warn: disk space below 15% on inference volume"
  "runtime info: model warm-up completed in 3.7 seconds"
  "runtime error: segmentation fault in inference engine"
  "runtime warn: socket connection closed unexpectedly"
  "runtime info: garbage collection completed in 452ms"
  "runtime error: failed to access cloud storage endpoint"
  "runtime warn: thread pool saturation detected"
  "runtime info: runtime optimization applied (SIMD enabled)"
  "runtime error: deadlock detected in worker process"
  "runtime warn: memory fragmentation at 35% threshold"
  "runtime info: graceful shutdown initiated by orchestrator"
  "runtime error: invalid model configuration parameters"
  "runtime warn: TLS certificate expiring in 7 days"
  "runtime info: vector database index rebuilt successfully" 
  "runtime error: RPC timeout between service components"
  "runtime warn: atomic operation failed, retrying (attempt 3/5)"
  "runtime info: concurrent requests peaked at 237 per minute"
  "runtime error: invalid tensor dimensions in model input"
  "runtime warn: preprocessing pipeline latency above threshold"
  "runtime info: horizontal scaling event triggered (+2 instances)"
  "runtime error: checksum verification failed for model weights"
  "runtime warn: rate limiting applied to client 192.168.1.45"
  "runtime info: session manager handling 156 active connections"
  "runtime error: dependency version conflict in container"
  "runtime warn: non-optimized path detected in compute graph"
  "runtime info: feature flag enabled: advanced_batching"
  "runtime error: failed to deserialize protobuf message"
  "runtime warn: deprecated API usage from client version 0.9.2"
  "runtime info: load balancer distribution optimized"
  "runtime error: service mesh routing configuration invalid"
  "runtime warn: approaching storage quota (89% used)"
  "runtime info: token validation cache refreshed"
  "runtime error: failed to initialize quantized model"
)

for i in {1..10}; do
  # Generate current timestamp with small random offset
  timestamp=$(date -u -v+${i}S +"%Y-%m-%dT%H:%M:%SZ")
  
  # Select random message
  message=${messages[$RANDOM % ${#messages[@]}]}
  
  # Generate log entry
  log_entry=$(cat <<EOF
{
	"message": "$message",
	"timestamp": "$timestamp",
	"logType": "runtimeLogs",
	"identifierId": "runtime-$(($RANDOM % 100))",
	"modelId": "mdl-999"
}
EOF
)
  
  # Send to Kinesis
  aws kinesis put-record \
    --stream-name inferless-logs-stream \
    --partition-key "mdl-999" \
    --data "$(echo "$log_entry" | base64)"
  
  # Small delay between requests
  sleep 0.5
done