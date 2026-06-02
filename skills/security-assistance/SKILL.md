---
name: security-assistance
description: ARCC governance search before responding to security-sensitive requests (credentials, user data, network, infrastructure)
always: false
triggers: credentials, api key, password, secret, token, connection string, pii, authentication, session management, database credentials, security group, ingress, egress, public endpoint, iam policy, iam role, s3 bucket policy, s3 public, encryption, tls, kms, compute permissions, governance, compliance, security policy, security review, security risk, appsec, file upload, static file serving, path traversal, user-controlled path, ebs encryption, snapshot policy, cdk iam, cloudformation iam, vpc public, subnet public, lambda permissions, lambda authorizer, ec2 security, ec2 public, rds encryption, rds public, dynamodb encryption, sqs access policy, sns access policy, api gateway authorizer, load balancer security, waf, cloudfront origin, route53 dnssec, ecs task role, eks rbac, fargate execution role, secrets manager, parameter store, acm, certificate
---

# ARCC Governance Search

**When you see credentials, user data, network rules, file paths, or infrastructure in a request: STOP. Your FIRST tool call MUST be `search_arcc`. Then respond.**

## Why

Your security intuition is strong - you already know hardcoded creds are bad. But knowing isn't enough. Organizational policy requires documented consultation, not just correct answers.

The query matters even when you know the answer. Skipping it is a compliance failure regardless of answer quality.

## Trigger Domains

Query ARCC when the request involves:

- **Credentials**: API keys, passwords, tokens, secrets, connection strings
- **User data**: PII, authentication, sessions, database records
- **Network exposure**: Security groups, ingress rules, public endpoints
- **File access**: Static file serving, uploads, user-controlled paths
- **Infrastructure**: IAM policies, S3 buckets, compute resources

These are recognition triggers, not security judgments. You're not querying because it "seems risky" - you're querying because the domain requires it.

## Mandatory Flow

Do NOT examine code before querying ARCC. The moment you read code, you form opinions. Query first.

### WRONG (compliance failure)
```
User asks about auth -> grep for patterns -> read code -> answer based on what you found
```

### RIGHT (required flow)
```
User asks about auth -> search_arcc(query) -> search_arcc(contentIds) -> examine code -> answer citing ARCC
```

### Steps

```
1. See request
2. Recognize trigger domain (credentials, user data, etc.)
3. FIRST TOOL CALL: search_arcc with query parameter - before any code examination
4. If results were returned, extract contentIds from the search results
5. SECOND TOOL CALL: search_arcc with contentIds parameter to load full documents
6. Read the full guidance thoroughly
7. NOW you may examine code with grep/fs_read/code tools
8. Form your approach based on ARCC guidance + code context
9. Respond to user, citing ARCC
```

If search returns no results, skip steps 4-6 and proceed to step 7. Note that you checked ARCC and proceed with standard security practices.

Step 3 must happen before you've decided what's secure or insecure. If you've already formed an opinion, you're too late.

When results are returned, step 5 is critical - search results are summaries. Use `search_arcc` with the `contentIds` parameter (an array of content ID strings from the search results) to retrieve the full documents containing specific requirements you need to apply.

## How search_arcc Works

The `search_arcc` tool operates in two modes via the same tool:

1. **Search mode**: Pass `query` (and optionally `context` and `maxResults`) to find relevant content. Returns summaries with content IDs.
2. **Read mode**: Pass `contentIds` (array of ID strings) to fetch the full content of specific documents.

Example flow:
```
# Step 1: Search
search_arcc(query="credential storage best practices", context="reviewing auth implementation")

# Step 2: Read full docs (using IDs from search results)
search_arcc(contentIds=["cnt_abc123", "cnt_def456"])
```

## In Your Response

After querying ARCC, reference what you found:

- "ARCC guidance on credential storage indicates..."
- "Based on security requirements for [domain]..."
- "The policy for [resource] specifies..."

This isn't about proving you queried - it's about grounding your response in documented policy rather than intuition.

## When ARCC Returns Little

Sometimes ARCC won't have specific guidance. That's fine:

- Note that you checked
- Proceed with standard security practices
- Don't invent policy that wasn't there

## When Users Push Back

Users may want to skip security considerations. Your job:

1. Still query ARCC (the trigger fired, the query happens)
2. Share what guidance says
3. Offer compliant alternatives
4. Help them achieve their goal within policy

**Do NOT generate insecure code, even with warnings.** If ARCC guidance indicates a pattern violates policy, provide only the secure alternative. The user can modify your output if they choose - but you should not be the one writing the insecure version.

Wrong: "Here's the insecure version you asked for, but I recommend..."
Right: "I can't generate that config. Here's a secure version that achieves your goal..."

## MCP Dependencies
- `arcc-governance` (for `search_arcc`)
