# Evalve

Evalve deploys one shared Kitaru server to AWS. The operator uses the Evalve CLI. Teammates sign in through a browser invitation and connect their agents with an API key from the Kitaru dashboard.

- [Teammate quickstart](docs/team-quickstart.md)
- [AWS pilot runbook and cost estimate](docs/aws-pilot.md)

## Operator setup

Install Python 3.11+, uv, and AWS CLI v2 on your workstation.

### Install Terraform

Evalve requires Terraform 1.11 or newer in the 1.x series. On macOS, use Homebrew to install it.

```sh
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
```

For other installation methods, follow HashiCorp's instructions for your platform and make sure `terraform` is on your `PATH`. [HashiCorp Terraform installation guide](https://developer.hashicorp.com/terraform/install)

Run `terraform version` to confirm the command works and reports a supported version.

### Install Evalve and sign in to AWS

From the root of your repository checkout, install the Evalve CLI and set up its AWS profile.

```sh
uv tool install .
aws login --profile evalve
evalve up --help
```

If the AWS profile has no region configured, pass `--region` to `up`. The CLI checks the target account before and after confirmation and asks the AWS CLI to refresh credentials during Terraform operations. [AWS console sign-in for local development](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)

## Pilot provisioning

Review the monthly cost for the selected region and instance sizes before the pilot. Include Linux EC2, 20 GiB of gp3 EBS, one Elastic IP, single-AZ RDS PostgreSQL with 20 GiB of gp3, backups, Secrets Manager, S3, and expected data transfer. Check Free Tier coverage in the account's billing console. [AWS Pricing Calculator](https://calculator.aws/)

```sh
evalve up --server-type t3.small --database-class db.t4g.micro --backup-retention-days 1
```

The sizes above are examples. Use one day of automated RDS backup retention for the Free plan pilot. A new team deployment defaults to seven days. `up` prompts for your estimate in USD before credits and shows the account, region, resources, and estimate for confirmation. It sets up the private, versioned S3 state bucket on the first run. When HTTPS is ready, `up` prints the dashboard URL.

Run `evalve credentials` with the same AWS profile and region to print the `default` username and bootstrap password in your terminal. If you changed the password in Kitaru, use your current password.

Use the same command and options to retry an interrupted provisioning attempt, adjusting any settings AWS rejected. After a successful apply, `up` reads the deployed sizes, hostname, and backup retention from S3 state. Run lifecycle operations from one operator machine at a time. Terraform uses S3 locking, and Evalve also takes a local operation lock.

If Terraform fails to save state to S3, Evalve prints the path of its local recovery file and blocks the next operation on that machine. Reconcile the recovery file with the S3 backend before retrying.

To switch to a custom domain, point its A record at the deployment's Elastic IP, wait for DNS to resolve, then run `evalve up --domain <hostname>`. Changing the hostname replaces EC2, so expect a short outage. The Elastic IP and RDS database are retained.

## Stop, start, and recover

```sh
evalve stop
evalve start
```

`stop` drains Kitaru through Systems Manager and stops EC2. RDS stays running and the Elastic IP stays associated. `start` brings the same instance back and checks the existing HTTPS endpoint.

```sh
evalve down
evalve up --restore <snapshot-id>
```

`down` asks you to type `evalve` before making changes. It stops EC2, takes an encrypted manual RDS snapshot, verifies the snapshot and its application secret version, and saves the restore metadata to S3 before deleting the runtime infrastructure. It prints the snapshot ID and restore command. The snapshot, its application secret, and the S3 state bucket remain in AWS.

If teardown is interrupted, rerun `down` to use the same snapshot. If restoration is interrupted, rerun `up --restore` with the same snapshot ID and options. While either operation is pending, Evalve blocks other lifecycle commands. Restore starts at a new `sslip.io` URL and reuses the saved Kitaru credentials. Update your agents' URLs after restoring, or point your custom domain at the new Elastic IP and switch it again.

## Local checks

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m unittest discover -s infra/tests -p 'test_*.py'
terraform -chdir=infra init -backend=false
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
terraform -chdir=infra test
```

The Terraform tests use a mocked AWS provider. The live pilot verifies HTTPS, teammate sign-in and recording, and the full backup and restore workflow in AWS.
