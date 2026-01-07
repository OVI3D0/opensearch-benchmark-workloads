# Vespa Deployment Guide for nyc_taxis Benchmark

## EC2 Setup

### 1. Launch EC2 Instance

**Recommended specs:**
- **Instance type**: `m5.2xlarge` (8 vCPU, 32GB RAM) or larger
- **Storage**: 100GB+ gp3 SSD
- **AMI**: Amazon Linux 2023 or Ubuntu 22.04
- **Security Group**: Open ports 8080, 19071 (and 22 for SSH)

### 2. Install Docker on EC2

```bash
# Amazon Linux 2023
sudo yum update -y
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER
# Log out and back in for group changes

# Ubuntu 22.04
sudo apt update
sudo apt install -y docker.io
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER
```

### 3. Start Vespa Container

```bash
# Pull and run Vespa
docker run --detach --name vespa \
  --hostname vespa-container \
  --publish 8080:8080 \
  --publish 19071:19071 \
  vespaengine/vespa

# Wait for config server to start (~30-60 seconds)
echo "Waiting for Vespa config server..."
until curl -s --head http://localhost:19071/state/v1/health | grep "200 OK"; do
  sleep 5
  echo "Still waiting..."
done
echo "Config server is up!"
```

### 4. Deploy the Application Package

**Option A: From local machine (copy files to EC2 first)**

```bash
# On your local machine, zip the app
cd /path/to/opensearch-benchmark-workloads/nyc_taxis/vespa-app
zip -r nyc_taxis_vespa.zip .

# Copy to EC2
scp -i your-key.pem nyc_taxis_vespa.zip ec2-user@<EC2_IP>:~/
```

**On EC2:**
```bash
# Unzip
unzip nyc_taxis_vespa.zip -d nyc_taxis_vespa

# Deploy to Vespa
cd nyc_taxis_vespa
zip -r ../app.zip .
curl --header "Content-Type: application/zip" \
  --data-binary @../app.zip \
  http://localhost:19071/application/v2/tenant/default/prepareandactivate

# Verify deployment
curl http://localhost:19071/application/v2/tenant/default/application/default
```

**Option B: Deploy directly using Vespa CLI**

```bash
# Install Vespa CLI
curl -fsSL https://raw.githubusercontent.com/vespa-engine/vespa/master/client/go/script/install-cli.sh | bash

# Deploy
cd nyc_taxis_vespa
vespa deploy --wait 300
```

### 5. Verify Vespa is Ready

```bash
# Check application status
curl http://localhost:8080/ApplicationStatus

# Check document API is ready
curl http://localhost:8080/document/v1/

# Test schema is deployed
curl "http://localhost:8080/search/?yql=select%20*%20from%20nyc_taxis%20where%20true"
```

### 6. Run the Benchmark

**From your benchmark client machine:**

```bash
# Set the OBJC flag if on macOS
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# Run benchmark against remote Vespa
opensearch-benchmark run \
  --pipeline=benchmark-only \
  --workload=nyc_taxis \
  --database-type=vespa \
  --target-hosts=<EC2_PUBLIC_IP>:8080 \
  --test-mode  # Remove for full run
```

## Quick One-Liner Setup (on EC2)

```bash
# Run this after Docker is installed
docker run -d --name vespa -p 8080:8080 -p 19071:19071 vespaengine/vespa && \
sleep 60 && \
mkdir -p app/schemas && \
cat > app/services.xml << 'EOF'
<?xml version="1.0" encoding="utf-8" ?>
<services version="1.0">
    <container id="default" version="1.0">
        <document-api/>
        <search/>
        <nodes><node hostalias="node1"/></nodes>
    </container>
    <content id="nyc_taxis" version="1.0">
        <redundancy>1</redundancy>
        <documents><document type="nyc_taxis" mode="index"/></documents>
        <nodes><node hostalias="node1" distribution-key="0"/></nodes>
    </content>
</services>
EOF
cat > app/hosts.xml << 'EOF'
<?xml version="1.0" encoding="utf-8" ?>
<hosts><host name="localhost"><alias>node1</alias></host></hosts>
EOF
cat > app/schemas/nyc_taxis.sd << 'EOF'
schema nyc_taxis {
    document nyc_taxis {
        field surcharge type float { indexing: summary | attribute }
        field mta_tax type float { indexing: summary | attribute }
        field tolls_amount type float { indexing: summary | attribute }
        field extra type float { indexing: summary | attribute }
        field improvement_surcharge type float { indexing: summary | attribute }
        field fare_amount type float { indexing: summary | attribute }
        field ehail_fee type float { indexing: summary | attribute }
        field total_amount type float { indexing: summary | attribute }
        field trip_distance type float { indexing: summary | attribute }
        field tip_amount type float { indexing: summary | attribute }
        field passenger_count type int { indexing: summary | attribute }
        field trip_type type string { indexing: summary | attribute }
        field rate_code_id type string { indexing: summary | attribute }
        field payment_type type string { indexing: summary | attribute }
        field vendor_id type string { indexing: summary | attribute }
        field store_and_fwd_flag type string { indexing: summary | attribute }
        field cab_color type string { indexing: summary | attribute }
        field vendor_name type string { indexing: summary | index }
        field pickup_datetime type string { indexing: summary | attribute }
        field dropoff_datetime type string { indexing: summary | attribute }
        field pickup_location type position { indexing: summary | attribute }
        field dropoff_location type position { indexing: summary | attribute }
    }
}
EOF
cd app && zip -r ../app.zip . && cd .. && \
curl --header "Content-Type: application/zip" --data-binary @app.zip \
  http://localhost:19071/application/v2/tenant/default/prepareandactivate
```

## Troubleshooting

### Check Vespa logs
```bash
docker logs vespa
docker exec vespa cat /opt/vespa/logs/vespa/vespa.log
```

### Restart deployment
```bash
docker restart vespa
# Wait and redeploy
```

### Memory issues
```bash
# Vespa needs at least 4GB RAM
# Check with: free -h
# For larger datasets, use m5.4xlarge or bigger
```
