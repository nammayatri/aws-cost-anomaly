# Cost Anomaly Cron

Daily AWS cost-anomaly report → Slack.

A Kubernetes CronJob that runs once a day, looks at yesterday's AWS bill broken down by service, flags services whose cost moved beyond your thresholds compared to the previous day **and** the same weekday last week, drills down by `USAGE_TYPE` (with actual usage quantity in GB / hours / requests, not just dollars), and posts a structured Slack message — a clean summary as the main message, with the per-service breakdown in a thread reply.

If nothing crosses the thresholds in either direction, the cron stays silent for the day. No noise on quiet days.

## What you get in Slack

**Main message** — date, total spend, vs day-before and vs same-day-last-week (with the baseline dollar amounts shown), and a one-line count of services that moved.

**Thread reply** — two sections: services that increased (orange/red) and services that decreased (green). Each card shows the cost delta with the actual comparison date, plus a few bullets naming the specific usage types responsible for the change with their before/after quantities.

## How it works

```
CronJob (default: 12:00 UTC)
   └─ Python container
        ├─ boto3 ce.get_cost_and_usage   (last 21 days, daily, GROUP BY SERVICE)
        ├─ for each service: detect deltas vs T-1 and vs T-7
        ├─ if any cross thresholds: second CE call per flagged service GROUP BY USAGE_TYPE
        │      (with UsageQuantity, so we can show GB / requests / hours moved)
        └─ slack_sdk.chat_postMessage → main + thread reply
```

## Repo layout

```
.
├── main.py, fetch.py, detect.py, drilldown.py, slack.py, config.py
├── requirements.txt
├── Dockerfile
├── config.example.json     # documents config.json shape, dummy values
├── k8s/                    # public-friendly manifest templates with <PLACEHOLDERS>
│   ├── cronjob.yaml
│   └── secret.yaml
└── prod/                   # gitignored — real values for your deployment
    ├── cronjob.yaml
    └── secret.yaml
```

`config.json` and `prod/` are gitignored. The committed `k8s/` manifests are placeholders only — copy them to `prod/` and fill in real values, then apply from `prod/`.

## Configuration

Two sources, merged with this precedence: **env vars > `config.json` > defaults**. Same code path for local and prod.

| Key (json) | Env var | Default | Meaning |
|---|---|---|---|
| `aws_profile` | `AWS_PROFILE` | — | Local only. In-cluster, IRSA/Workload Identity is used instead. |
| `aws_region` | `AWS_REGION` | `ap-south-1` | Region for Cost Explorer client. |
| `slack_bot_token` | `SLACK_BOT_TOKEN` | — | Bot token (`xoxb-…`) with `chat:write` and `chat:write.public`. **Required.** |
| `slack_channel_id` | `SLACK_CHANNEL_ID` | — | Channel ID (prefer ID over `#name` so renames don't break things). **Required.** |
| `mention` | `MENTION` | empty | `here` / `channel` / a user ID (`U…`) / a usergroup ID (`S…`). Empty = no mention. |
| `increase_pct_threshold` | `INCREASE_PCT_THRESHOLD` | `10` | A service must move > this percent **up** (vs T-1 or T-7) to be flagged. |
| `decrease_pct_threshold` | `DECREASE_PCT_THRESHOLD` | `5` | And > this percent **down** to be flagged on the decrease side. |
| `abs_threshold` | `ABS_THRESHOLD` | `1` | Plus an absolute-dollar guard so penny moves don't fire. |
| `noise_floor` | `NOISE_FLOOR` | `1` | Skip a service entirely if its max value across the three days is below this. |
| `lookback_days` | `LOOKBACK_DAYS` | `21` | How much history to fetch (must be ≥ 8). |
| `top_usage_types` | `TOP_USAGE_TYPES` | `3` | How many usage types to show in the drill-down per flagged service. |

## Local run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json
# edit config.json with your slack token + channel id + aws_profile

python main.py --dry-run                       # prints Slack payload to stdout
python main.py --csv /path/to/cost.csv --date 2026-05-15 --dry-run   # backtest against a CSV
python main.py                                 # post live
```

`python main.py --csv …` skips the AWS API entirely — useful if you only have an exported CSV and want to see what the report would look like.

## AWS IAM

The pod's IAM role (via IRSA on EKS, or Workload Identity on GKE) only needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["ce:GetCostAndUsage"],
    "Resource": "*"
  }]
}
```

Cost Explorer doesn't support resource-level ARNs, so `"Resource": "*"` is unavoidable, but the action itself is read-only.

If you already have an IAM role for cost/monitoring work, you can reuse its service account directly — no need to provision a second role.

## Deploy

1. **Image** — build and push to your container registry of choice. Example for AWS ECR:

   ```bash
   aws ecr create-repository --repository-name cost-anomaly-cron --region <REGION>

   docker buildx build --platform linux/amd64 -t cost-anomaly-cron:v1 .
   docker tag cost-anomaly-cron:v1 <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/cost-anomaly-cron:v1

   aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com
   docker push <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/cost-anomaly-cron:v1
   ```

2. **Prod manifests** — copy `k8s/*.yaml` into `prod/`, fill in real namespace, service account, image tag, channel ID, and bot token:

   ```bash
   cp k8s/cronjob.yaml prod/cronjob.yaml
   cp k8s/secret.yaml  prod/secret.yaml
   # edit both with your real values
   ```

3. **Apply**:

   ```bash
   kubectl apply -f prod/secret.yaml
   kubectl apply -f prod/cronjob.yaml
   ```

4. **Smoke-test**:

   ```bash
   kubectl -n <NS> create job --from=cronjob/cost-anomaly-cron cost-anomaly-test-1
   kubectl -n <NS> logs -f job/cost-anomaly-test-1
   ```

   You should see a Slack post (or `No threshold crossings — skipping Slack post.` in the logs).

5. **Tune** thresholds after a few days based on signal-to-noise.

## Schedule

Default `0 12 * * *` UTC. Pick a time when yesterday's billing data has fully settled in Cost Explorer — usually 12:00 UTC is safe. If you run earlier, you may see partial numbers and the cron may flag false anomalies.
