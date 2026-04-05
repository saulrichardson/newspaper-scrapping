# AWS Account Reference

This is the safe tracked account reference. It documents:

- which account-specific values matter
- how to rediscover them
- which values belong only in the local private handoff note

It intentionally does **not** publish the live account's exact bucket names,
instance IDs, IPs, subnet IDs, security group IDs, or operator endpoints.

For the exact current values on this machine, use the gitignored local file:

- `docs/private/aws_account_reference.local.md`

## What Goes Where

### Safe to keep in tracked docs

- the categories of values that matter
- the discovery commands for those values
- the runbooks that use those values
- the generic launch/update/recovery flow

### Keep only in the local private reference

- exact account IDs if you do not want them published
- exact bucket names
- exact SNS topic ARNs and subscriber endpoints
- exact instance IDs and public IPs
- exact subnet, VPC, and security group IDs
- exact key-pair names and local key paths
- exact current runtime prefixes
- exact current fleet inventory

### Never store in git

- Newspapers.com credentials
- private SSH key material
- DCV passwords
- cookie file contents

## Values A New Operator Must Know Exist

The local private reference should maintain these fields:

| Category | Field |
| --- | --- |
| Control plane | AWS account ID |
| Control plane | default region |
| Control plane | default AZ used by workers |
| EC2 | worker AMI ID |
| EC2 | worker AMI name |
| EC2 | subnet ID |
| EC2 | VPC ID |
| EC2 | worker security group ID and name |
| EC2 | key-pair name |
| Local operator | SSH private key path |
| IAM | instance profile name |
| IAM | EC2 role name |
| S3 | fleet bucket |
| S3 | preview bucket |
| SNS | operator alert topic ARN |
| SNS | subscriber endpoint(s) |
| Runtime | current active results prefix |
| Runtime | canonical archive prefix |
| Runtime | inventory prefix |
| Fleet | current active workers |
| Fleet | current retired-but-retained workers |
| Inputs | known-good cookie seed key |
| Inputs | known-good bundle key |

## Discovery Commands

These are the exact commands a new operator should use to rebuild the local
private reference when values change.

### Control plane identity

```bash
aws sts get-caller-identity
```

### Buckets

```bash
aws s3api list-buckets --query 'Buckets[].Name' --output table
```

### Topics and subscribers

```bash
aws sns list-topics --region "$AWS_REGION" --query 'Topics[].TopicArn' --output table
aws sns list-subscriptions-by-topic \
  --region "$AWS_REGION" \
  --topic-arn "$SNS_TOPIC_ARN"
```

### Fleet discovery

```bash
aws ec2 describe-instances \
  --region "$AWS_REGION" \
  --filters \
    Name=instance-state-name,Values=pending,running,stopping,stopped \
    Name=key-name,Values="$KEY_NAME" \
  --query 'Reservations[].Instances[].{
    InstanceId:InstanceId,
    Name:Tags[?Key==`Name`]|[0].Value,
    Role:Tags[?Key==`Role`]|[0].Value,
    Project:Tags[?Key==`Project`]|[0].Value,
    State:State.Name,
    Type:InstanceType,
    PrivateIp:PrivateIpAddress,
    PublicIp:PublicIpAddress,
    Subnet:SubnetId,
    Vpc:VpcId,
    Az:Placement.AvailabilityZone,
    ImageId:ImageId,
    KeyName:KeyName,
    IamProfile:IamInstanceProfile.Arn,
    SecurityGroups:SecurityGroups[].GroupId,
    LaunchTime:LaunchTime
  }' \
  --output table
```

### AMI details

```bash
aws ec2 describe-images \
  --region "$AWS_REGION" \
  --image-ids "$AMI_ID" \
  --query 'Images[0].{ImageId:ImageId,Name:Name,CreationDate:CreationDate,Description:Description}'
```

### Security group and access rules

```bash
aws ec2 describe-security-groups \
  --region "$AWS_REGION" \
  --group-ids "$WORKER_SECURITY_GROUP_ID"
```

### IAM role and instance profile

```bash
aws iam get-role --role-name "$WORKER_ROLE_NAME"
aws iam list-role-policies --role-name "$WORKER_ROLE_NAME"
aws iam get-role-policy \
  --role-name "$WORKER_ROLE_NAME" \
  --policy-name "$WORKER_ROLE_POLICY_NAME"
aws iam list-instance-profiles-for-role --role-name "$WORKER_ROLE_NAME"
```

### Bundles, plans, and cookie seeds

```bash
aws s3 ls "s3://$FLEET_BUCKET/bundles/"
aws s3 ls "s3://$FLEET_BUCKET/plans/" --recursive
aws s3 ls "s3://$FLEET_BUCKET/state/cookies/"
```

### Current worker launch inputs

Use this to recover the original launch user-data for a specific instance:

```bash
aws ec2 describe-instance-attribute \
  --region "$AWS_REGION" \
  --instance-id "$INSTANCE_ID" \
  --attribute userData \
  --query 'UserData.Value' \
  --output text \
| base64 --decode
```

Important:

- user-data shows the original launch inputs
- it may not reflect the live runtime prefix after a manual reseat

## Runtime Prefix Discovery

To discover candidate results prefixes:

```bash
aws s3 ls "s3://$FLEET_BUCKET/results/"
```

To verify that a candidate prefix is still active:

```bash
aws s3 ls "s3://$FLEET_BUCKET/$RESULTS_PREFIX/" --recursive | tail -n 20
```

Do not assume the watcher prefix from memory. Verify it.

## Minimum Operator Permissions

There is still no tracked operator IAM policy document in this repo. The human
operator currently needs enough AWS access to do at least the following:

- EC2
  - `DescribeInstances`
  - `DescribeImages`
  - `DescribeVolumes`
  - `DescribeSecurityGroups`
  - `DescribeInstanceAttribute`
  - `RunInstances`
  - `StartInstances`
  - `StopInstances`
  - `RebootInstances`
  - `TerminateInstances`
  - `CreateTags`
  - `CreateVolume`
  - `AttachVolume`
  - `DeleteVolume`
  - `AuthorizeSecurityGroupIngress`
  - `RevokeSecurityGroupIngress`
- IAM
  - `GetRole`
  - `ListRolePolicies`
  - `GetRolePolicy`
  - `ListInstanceProfilesForRole`
  - `PassRole` on the worker role
- S3
  - `ListAllMyBuckets`
  - `ListBucket`
  - `GetObject`
  - `PutObject`
  - `DeleteObject`
- SNS
  - `ListTopics`
  - `ListSubscriptionsByTopic`
  - `Publish`
- SESv2
  - optional, only if HTML mail is used

## Related Documents

- [docs/aws.md](aws.md)
- [docs/aws_launch_runbook.md](aws_launch_runbook.md)
- [docs/aws_operations_runbook.md](aws_operations_runbook.md)
- [docs/aws_storage_model.md](aws_storage_model.md)
