# Evalve AWS pilot

Use the separate Free Tier pilot account and synthetic traces. The team-owned deployment gets a fresh database after the pilot passes.

## Account and cost review

Install Evalve from the repository as described in the [operator setup](../README.md). Sign in to the pilot account in the browser and verify the account ID.

```sh
aws login --profile evalve
aws sts get-caller-identity --profile evalve --query Account --output text
aws freetier get-account-plan-state --profile evalve --region us-east-1
```

The last command reports the account plan, status, and available credits if the profile has billing access. You can also check those details in the AWS Billing console. Confirm that the account can provision the intended region and resource types before approving `up`. [AWS Free Tier plan check](https://docs.aws.amazon.com/cli/latest/reference/freetier/get-account-plan-state.html)

The proposed pilot uses Linux `t3.small` with 2 GiB RAM and PostgreSQL `db.t4g.micro` in `us-east-1`. The public on-demand rates below were checked November 10, 2026 and assume 730 hours of runtime, one Elastic IP, and 20 GiB each of EC2 and RDS storage.

| Resource | Estimated monthly cost before credits | Rate source |
| --- | ---: | --- |
| EC2 `t3.small` at $0.0208/hour | $15.18 | [AWS EC2 regional price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.csv) |
| EC2 gp3 storage | $1.60 | [Amazon EBS pricing](https://aws.amazon.com/ebs/pricing/) |
| Elastic IP | $3.65 | [Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/) |
| Single-AZ RDS `db.t4g.micro` at $0.016/hour | $11.68 | [AWS RDS regional price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-1/index.csv) |
| RDS gp3 storage | $2.30 | [AWS RDS regional price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-1/index.csv) |
| Two Secrets Manager secrets and two reads per minute | $1.24 | [AWS Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/) |
| **Baseline** | **$35.65** | |

S3 requests and stored state, retained snapshot storage above the RDS allowance, surplus CPU credits, taxes, and metered data transfer can add to the baseline. Review the estimate again if the pilot account requires another region or instance type. [AWS Pricing Calculator](https://calculator.aws/) · [RDS backup storage](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html)

## Deploy and verify access

```sh
evalve up --region us-east-1 --server-type t3.small --database-class db.t4g.micro \
  --backup-retention-days 1 --monthly-estimate 36.00
```

Use one day of automated RDS backup retention for this Free plan pilot. The pilot account rejected the seven-day team default. Manual snapshots created by `evalve down` are separate from the automated backup retention setting.

At the prompt, approve only after the displayed account, region, backup retention, and estimate match the pilot review. `up` prints the HTTPS address when Kitaru passes its database and authentication checks.

1. Run `evalve credentials --profile evalve --region us-east-1`, then use the displayed bootstrap login at the HTTPS address.
2. Open **Settings → Members → Create User**, enter a pilot username, select **Invite User**, and copy the invitation link. Share the link directly with the pilot teammate along with the [teammate quickstart](team-quickstart.md).
3. Ask the teammate to set a password, sign in, create their own API key, and record one synthetic session. Record the agent name and session ID for the recovery drill. Keep credentials and invitation links out of the test log.
4. Confirm the signed-in teammate can open the session and see its input, output, and recorded steps. Verify unauthenticated API access returns HTTP 401.

## Recovery drill

| Check | Pass condition |
| --- | --- |
| `evalve stop`, then `evalve start` | The same HTTPS address returns, and the pilot login, key, and trace still work. |
| Optional custom hostname | Point an A record at the existing Elastic IP, then run `evalve up --region us-east-1 --domain <hostname> --monthly-estimate 36.00`. The custom HTTPS address serves the same trace. This check needs a DNS name you control. |
| `evalve down` | The CLI prints a verified snapshot ID and restore command. The EC2 instance, RDS instance, and Elastic IP are removed. The snapshot, application secret, and S3 state bucket remain. |
| `evalve up --restore <snapshot-id>` | A new HTTPS address comes up. The original teammate login and API key still authenticate, and the original session ID and trace payloads match. |
| Finish the pilot | Run `evalve down` again when testing is complete. Note the retained snapshots and any ongoing storage or secret charges. |

Pass `--region us-east-1` to lifecycle commands when the profile has no default region. Use the exact restore command printed by `down`, and review the displayed cost again before confirming the restore.

## Release and handoff

Record the outcome of each check before tagging a release. Build the wheel and source archive with `uv build`, then install the release into a clean environment and verify `evalve --help` and `evalve up --help`. Publish the tested tag in the operator's GitHub repository and send the teammate quickstart with the invitation from the new team deployment.
