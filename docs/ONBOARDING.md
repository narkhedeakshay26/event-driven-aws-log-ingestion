# Onboarding guide for AWS account admins

This is the page to send with the onboarding link. It takes about 5 minutes and creates
only resources named `log-ingest-<account>-<region>`.

## Before you start

- You need permission to create CloudFormation stacks, IAM roles, SQS queues and Lambda
  functions.
- **Use the account and region where the logs are stored.** With AWS Organizations or
  Control Tower, CloudTrail and Config logs often live in a separate **log archive**
  account. If your logs are in several accounts or regions, repeat these steps in each.
  The link works for all of them.

## Steps

1. Sign in to the AWS console for the account that stores the logs.
2. Open the **onboarding link** you were sent. It opens CloudFormation's **Quick create
   stack** page, with the template and access settings already filled in.
3. Check the region in the top-right corner is the one your logs are in, and change it
   if not.
4. Under **Which logs to share**, choose **Yes** or **No** for each log type. The stack
   finds where they're stored; you don't need bucket names. Use **Other buckets** only
   for logs AWS doesn't report, for example files copied in by Firehose.
5. Under **Pick up new log sources automatically every day?**, choose:
   - **Yes:** new trails, flow logs or load balancer logs you add later are shared
     within a day, with nothing to do.
   - **No:** nothing changes after today unless you update the stack.
6. Leave **Access** as it is. Tick **"I acknowledge that AWS CloudFormation might create
   IAM resources with custom names"** and choose **Create stack**.
7. Wait for **CREATE_COMPLETE** (1–2 minutes), then open the **Outputs** tab:
   - **WiredBuckets** lists what's now being shared.
   - **SkippedSources** lists logs found but stored in another account or region. If
     it's not `none`, repeat these steps there.
8. Reply with the **AccountId** and **Region** values. They aren't secrets.

## Later

- **Share more or fewer log types:** open the stack, choose **Update → Use existing
  template**, change the dropdowns and submit.
- **Share a new source now, instead of waiting for the daily scan:** run the command in
  the **RescanNow** output, or open the Lambda function it names and choose **Test**.
- **Stop sharing:** delete the stack. That removes the queue, the role and every
  notification it added. Notifications you had before are left alone.

## What the stack does in your account

- Reads where your logs are delivered, using describe calls only.
- Creates one encrypted SQS queue and a dead-letter queue.
- Adds a "new object" notification to each discovered log bucket, alongside any you
  already have.
- Creates a role that only the ingesting account can assume, and only with your
  organization's External ID. It can read objects in the discovered buckets and nothing
  else.

## If it fails

| Message | Meaning | Fix |
|---|---|---|
| `TrustedPrincipalArn is required` / `ExternalId is required` | The template was opened without the link | Use the link you were sent |
| `Configurations overlap` | The bucket already sends new-object events for the same prefix elsewhere | Tell your contact which bucket; they'll suggest a narrower setup |
| `Access Denied` on a bucket | A bucket policy blocks notification changes | Allow `s3:PutBucketNotification` for the stack, or run it as a bucket admin |
| `SkippedSources` isn't `none` | Some logs are stored in another account or region | Repeat the steps there with the same link |
