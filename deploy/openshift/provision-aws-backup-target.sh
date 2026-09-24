#!/usr/bin/env bash
# Provision the AWS/ROSA object store and IAM role used by the EnMaaS CNPG profile.
# This script intentionally does not create or copy Kubernetes Secrets containing AWS keys.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v aws >/dev/null || die "aws CLI is required"

: "${AWS_REGION:?Set AWS_REGION to the OpenShift cluster region}"
: "${S3_BUCKET:?Set S3_BUCKET to the dedicated backup bucket name}"
: "${OIDC_PROVIDER_HOST:?Set OIDC_PROVIDER_HOST without https://}"

ROLE_NAME="${ROLE_NAME:-pricetag-enmaas-cnpg-backup}"
ROLE_SUBJECT="${ROLE_SUBJECT:-system:serviceaccount:enmaas:aigateway-pg}"
OIDC_AUDIENCE="${OIDC_AUDIENCE:-openshift}"
S3_RETENTION_DAYS="${S3_RETENTION_DAYS:-30}"
[[ "$S3_RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]] || \
  die "S3_RETENTION_DAYS must be a positive integer"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
PROVIDER_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER_HOST}"

aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null || \
  die "OIDC provider is not registered: $PROVIDER_ARN"

if ! aws s3api head-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION" >/dev/null 2>&1; then
  if aws s3api get-bucket-location --bucket "$S3_BUCKET" --region "$AWS_REGION" >/dev/null 2>&1; then
    die "bucket exists but head-bucket was denied: $S3_BUCKET"
  fi
  if [[ "$AWS_REGION" == us-east-1 ]]; then
    if ! aws s3api create-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION"; then
      die "unable to create or access bucket: $S3_BUCKET"
    fi
  else
    if ! aws s3api create-bucket \
        --bucket "$S3_BUCKET" \
        --region "$AWS_REGION" \
        --create-bucket-configuration LocationConstraint="$AWS_REGION"; then
      die "unable to create or access bucket: $S3_BUCKET"
    fi
  fi
fi

BUCKET_REGION="$(aws s3api get-bucket-location --bucket "$S3_BUCKET" \
  --region "$AWS_REGION" --query LocationConstraint --output text)"
[[ "$BUCKET_REGION" == None ]] && BUCKET_REGION=us-east-1
[[ "$BUCKET_REGION" == "$AWS_REGION" ]] || \
  die "bucket $S3_BUCKET is in $BUCKET_REGION, expected $AWS_REGION"

aws s3api put-public-access-block \
  --bucket "$S3_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-ownership-controls \
  --bucket "$S3_BUCKET" \
  --ownership-controls Rules="[{ObjectOwnership=BucketOwnerEnforced}]"

aws s3api put-bucket-encryption \
  --bucket "$S3_BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-bucket-versioning \
  --bucket "$S3_BUCKET" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-lifecycle-configuration \
  --bucket "$S3_BUCKET" \
  --lifecycle-configuration \
  "{\"Rules\":[{\"ID\":\"backup-retention\",\"Status\":\"Enabled\",\"Filter\":{\"Prefix\":\"\"},\"Expiration\":{\"Days\":$S3_RETENTION_DAYS},\"NoncurrentVersionExpiration\":{\"NoncurrentDays\":$S3_RETENTION_DAYS},\"AbortIncompleteMultipartUpload\":{\"DaysAfterInitiation\":7}}]}"

TRUST_FILE="$(mktemp)"
POLICY_FILE="$(mktemp)"
trap 'rm -f "$TRUST_FILE" "$POLICY_FILE"' EXIT

cat > "$TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "$PROVIDER_ARN"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "ForAnyValue:StringEquals": {
        "$OIDC_PROVIDER_HOST:aud": "$OIDC_AUDIENCE"
      },
      "StringEquals": {
        "$OIDC_PROVIDER_HOST:sub": "$ROLE_SUBJECT"
      }
    }
  }]
}
EOF

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-document "file://$TRUST_FILE"
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --description "CNPG backups for the PriceTag EnMaaS environment" \
    --assume-role-policy-document "file://$TRUST_FILE" >/dev/null
fi

cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": "arn:aws:s3:::$S3_BUCKET"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:AbortMultipartUpload",
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:ListMultipartUploadParts",
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::$S3_BUCKET/*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name cnpg-backup-bucket \
  --policy-document "file://$POLICY_FILE"

ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)"
printf 'AWS backup target ready\nBucket: %s\nRegion: %s\nRetentionDays: %s\nRole: %s\nSubject: %s\nAudience: %s\n' \
  "$S3_BUCKET" "$AWS_REGION" "$S3_RETENTION_DAYS" "$ROLE_ARN" "$ROLE_SUBJECT" "$OIDC_AUDIENCE"
