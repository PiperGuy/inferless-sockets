#!/usr/bin/env bash
set -euo pipefail

###
###  user config
###
REGION="us-east-1"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
STREAM_NAME="model-logs"
TABLE_NAME="ConnectionTable"
GSI_NAME="ModelIndex"
API_NAME="ModelLogSocket"
ROLE_NAME="websocketRole"
LAMBDA_AUTH="WebsocketAuthorizerLambda"
LAMBDA_CONSUMER="WebsocketConsumerLambda"
LAMBDA_REGISTRAR="WebsocketRegistrarLambda"
JWT_SECRET="my-demo-secret"   # ← replace with your actual secret

###
### 1) Create Kinesis stream
###
echo "→ Creating Kinesis stream: $STREAM_NAME"
aws kinesis create-stream \
  --stream-name "$STREAM_NAME" \
  --shard-count 1
aws kinesis wait stream-exists --stream-name "$STREAM_NAME"

###
### 2) Create DynamoDB table + GSI
###
echo "→ Creating DynamoDB table: $TABLE_NAME with GSI on modelId"
aws dynamodb create-table \
  --table-name "$TABLE_NAME" \
  --attribute-definitions \
      AttributeName=PK,AttributeType=S \
      AttributeName=modelId,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --global-secondary-indexes '[
    {
      "IndexName":"'"$GSI_NAME"'",
      "KeySchema":[{"AttributeName":"modelId","KeyType":"HASH"}],
      "Projection":{"ProjectionType":"ALL"}
    }
  ]'
aws dynamodb wait table-exists --table-name "$TABLE_NAME"

###
### 3) Create IAM execution role for all Lambdas
###
echo "→ Creating IAM role: $ROLE_NAME"
cat > /tmp/trust-policy.json <<EOF
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Principal":{"Service":"lambda.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document file:///tmp/trust-policy.json

# attach basic execution policy
aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# attach inline policy for DynamoDB, Kinesis, ManageConnections & invoke-permission
cat > /tmp/socket-policy.json <<EOF
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Action":[
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query"
      ],
      "Resource":[
        "arn:aws:dynamodb:$REGION:$ACCOUNT:table/$TABLE_NAME",
        "arn:aws:dynamodb:$REGION:$ACCOUNT:table/$TABLE_NAME/index/$GSI_NAME"
      ]
    },
    {
      "Effect":"Allow",
      "Action":[
        "kinesis:DescribeStream",
        "kinesis:GetShardIterator",
        "kinesis:GetRecords"
      ],
      "Resource":"arn:aws:kinesis:$REGION:$ACCOUNT:stream/$STREAM_NAME"
    },
    {
      "Effect":"Allow",
      "Action":"execute-api:ManageConnections",
      "Resource":"arn:aws:execute-api:$REGION:$ACCOUNT:*/*/@connections/*"
    },
    {
      "Effect":"Allow",
      "Action":"lambda:InvokeFunction",
      "Resource":"arn:aws:lambda:$REGION:$ACCOUNT:function:$LAMBDA_CONSUMER*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name SocketInfraPolicy \
  --policy-document file:///tmp/socket-policy.json

###
### 4) Package & create Lambdas
###
echo "→ Creating Authorizer Lambda: $LAMBDA_AUTH"
aws lambda create-function \
  --function-name "$LAMBDA_AUTH" \
  --runtime python3.12 \
  --role arn:aws:iam::$ACCOUNT:role/$ROLE_NAME \
  --handler authorizer.lambda_handler \
  --zip-file fileb://authorizer.zip \
  --timeout 10 \
  --environment Variables="{JWT_SECRET=$JWT_SECRET}"

echo "→ Creating Consumer Lambda: $LAMBDA_CONSUMER"
aws lambda create-function \
  --function-name "$LAMBDA_CONSUMER" \
  --runtime python3.12 \
  --role arn:aws:iam::$ACCOUNT:role/$ROLE_NAME \
  --handler consumer.handler \
  --zip-file fileb://consumer.zip \
  --timeout 300

echo "→ Creating Registrar Lambda: $LAMBDA_REGISTRAR"
aws lambda create-function \
  --function-name "$LAMBDA_REGISTRAR" \
  --runtime python3.12 \
  --role arn:aws:iam::$ACCOUNT:role/$ROLE_NAME \
  --handler registrar.handler \
  --zip-file fileb://registrar.zip \
  --timeout 60 \
  --environment Variables="{TABLE=$TABLE_NAME,CONSUMER_ARN=arn:aws:lambda:$REGION:$ACCOUNT:function:$LAMBDA_CONSUMER}"

###
### 5) Create the WebSocket API
###
echo "→ Creating WebSocket API: $API_NAME"
API_ID=$(aws apigatewayv2 create-api \
  --name "$API_NAME" \
  --protocol-type WEBSOCKET \
  --route-selection-expression '$request.body.action' \
  --query ApiId --output text)

###
### 6) Create Lambda integrations
###
echo "→ Creating integration for Registrar Lambda"
INTEGRATION_ID=$(aws apigatewayv2 create-integration \
  --api-id "$API_ID" \
  --integration-type AWS_PROXY \
  --integration-uri arn:aws:lambda:$REGION:$ACCOUNT:function:$LAMBDA_REGISTRAR \
  --payload-format-version 2.0 \
  --query IntegrationId --output text)

###
### 7) Create the REQUEST authorizer
###
echo "→ Creating REQUEST authorizer"
AUTH_ID=$(aws apigatewayv2 create-authorizer \
  --api-id "$API_ID" \
  --name "$LAMBDA_AUTH" \
  --authorizer-type REQUEST \
  --identity-source 'route.request.querystring.token' \
  --authorizer-uri "arn:aws:apigateway:$REGION:lambda:path/2015-03-31/functions/arn:aws:lambda:$REGION:$ACCOUNT:function:$LAMBDA_AUTH/invocations" \
  --query AuthorizerId --output text)

###
### 8) Grant API Gateway permission to invoke the Lambdas
###
echo "→ Granting API Gateway invoke perms"
aws lambda add-permission \
  --function-name "$LAMBDA_AUTH" \
  --statement-id apigw-auth \
  --principal apigateway.amazonaws.com \
  --action lambda:InvokeFunction \
  --source-arn arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/authorizers/* || true

aws lambda add-permission \
  --function-name "$LAMBDA_REGISTRAR" \
  --statement-id apigw-proxy \
  --principal apigateway.amazonaws.com \
  --action lambda:InvokeFunction \
  --source-arn arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/* || true

###
### 9) Create routes & attach integrations/authorizer
###
echo "→ Creating routes"
# $connect
aws apigatewayv2 create-route \
  --api-id "$API_ID" \
  --route-key \$connect \
  --authorization-type CUSTOM \
  --authorizer-id "$AUTH_ID" \
  --target integrations/$INTEGRATION_ID

# $disconnect
aws apigatewayv2 create-route \
  --api-id "$API_ID" \
  --route-key \$disconnect \
  --authorization-type NONE \
  --target integrations/$INTEGRATION_ID

# streamLogs
aws apigatewayv2 create-route \
  --api-id "$API_ID" \
  --route-key streamLogs \
  --authorization-type NONE \
  --target integrations/$INTEGRATION_ID

# stopStream
aws apigatewayv2 create-route \
  --api-id "$API_ID" \
  --route-key stopStream \
  --authorization-type NONE \
  --target integrations/$INTEGRATION_ID

###
### 10) Deploy to production stage (auto-deploy)
###
echo "→ Deploying stage production"
aws apigatewayv2 create-stage \
  --api-id "$API_ID" \
  --stage-name production \
  --auto-deploy

echo "WebSocket URL: wss://$API_ID.execute-api.$REGION.amazonaws.com/production"

###
### 11) Wire up the Consumer Lambda event-source mapping
###
echo "→ Creating event-source mapping for Consumer Lambda"
aws lambda create-event-source-mapping \
  --function-name "$LAMBDA_CONSUMER" \
  --batch-size 100 \
  --starting-position TRIM_HORIZON \
  --event-source-arn arn:aws:kinesis:$REGION:$ACCOUNT:stream/$STREAM_NAME

echo "✅ All infra created successfully!"  
