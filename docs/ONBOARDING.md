# Onboarding guide for account owners

This is the page to send to the person who administers the AWS account where the logs are
stored. It takes about 5 minutes and touches only resources named `log-ingest-<tag>`.

## Before you start

- You need permission to create CloudFormation stacks, IAM roles, SQS queues and Lambda
  functions in this account.
- **Use the account that owns the log buckets.** With AWS Organizations or Control Tower,
  CloudTrail and Config logs often live in a separate **log archive** account, not in the
  account where your workloads run.
- **Use the bucket's region.** The stack must be in the same region as every bucket it lists.

## Steps

1. Sign in to the AWS console for the account that owns the buckets, and choose the
   buckets' region in the top-right corner.
2. Open **CloudFormation → Create stack → With new resources (standard)**.
3. Choose **Upload a template file**, select the file you were sent (for example
   `acme-prod-onboarding.yaml`), and choose **Next**.
4. For **Stack name**, type the name you were given (for example `log-ingest-acme-prod`).
   The parameters are already filled in, so check that the bucket names look right, then
   choose **Next**.
5. Choose **Next** again. Tick **"I acknowledge that AWS CloudFormation might create IAM
   resources with custom names"** and choose **Submit**.
6. Wait for **CREATE_COMPLETE** (1–2 minutes), then open the **Outputs** tab.
7. Send back **QueueUrl** and **ReaderRoleArn**. They aren't secrets and are safe to email.

If you were given an **AccessKey**-mode template instead, the Outputs show a
`ReaderAccessKeySecretArn`. Open it in **Secrets Manager** and share its value **only
through the secure channel** agreed with your contact, never by email or chat.

## Later: adding or removing a bucket

Open the **same** stack, choose **Update → Use existing template → Next**, edit
**BucketNames** (and **BucketPrefixes** if one is set), then submit. Don't create a second
stack. Let your contact know, so their side lists the same buckets.

## What the stack does in your account

- Creates one SQS queue and a dead-letter queue, both encrypted.
- Adds an event notification (new objects only) to each listed bucket, alongside any
  notifications you already have.
- Creates a role that your contact's account can assume only with the agreed External ID.
  It can read objects in the listed buckets and prefixes, and nothing else in the account.
- Deleting the stack removes all of this, including the bucket notifications.

## If it fails

| Message | Meaning | Fix |
|---|---|---|
| `BucketPrefixes has N entries, BucketNames has M` | Prefix list doesn't line up with the buckets | Use `NONE`, or one entry per bucket in the same order |
| `Configurations overlap` | The bucket already sends new-object events for this prefix somewhere else | Ask your contact to set a narrower prefix |
| `Unable to validate the following destination configurations` | Bucket is in another region, or in another account | Deploy in the bucket's region and account |
| `Access Denied` on a bucket | The bucket policy blocks notification changes | Temporarily allow `s3:PutBucketNotification` for the stack, or apply it as a bucket admin |
