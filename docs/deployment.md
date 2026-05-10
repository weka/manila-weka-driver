# Step-by-Step Deployment Guide

This guide walks you through setting up the Manila host, then installing
and configuring the Manila Weka driver from scratch.  It assumes you have
a running OpenStack environment (controller, compute, and networking
already working) and a Weka storage cluster on your network.
No prior experience with Manila or Manila drivers is required.

---

## Before You Begin

### What you need

| Component | Minimum version | Where to check |
|-----------|----------------|----------------|
| OpenStack (controller up) | 2023.1 (Antelope) | `openstack token issue` |
| Weka cluster | 5.0 (tested against 5.1.x; v4.x not validated) | Weka GUI → About |
| Manila host OS | RHEL 8+ / Ubuntu 20.04+ | `cat /etc/os-release` |
| Python | 3.9+ | `python3 --version` |
| MariaDB / MySQL | 10.4+ | `mysql --version` |
| RabbitMQ | 3.8+ | `rabbitmqctl status` |
| Network access | Manila host → Weka cluster port 14000 | `curl` test below |

### What "Manila host" means

The **Manila host** is the Linux server that runs the `openstack-manila-share`
(or `manila-share`) service.  This is where you will install the driver and
the WekaFS kernel module.  It is **not** the Weka storage nodes themselves.

If you are unsure which server this is, run the following from your
OpenStack controller:

```bash
openstack share service list
```

Look for a line containing `manila-share` — the `Host` column shows the
hostname.

---

## Environment Variables

**Set these once before running any command in this guide.**  Every
command below uses these variables so you never have to edit a placeholder
manually.

Open a terminal on the Manila host (or your workstation for the
controller-side steps) and paste the block below, filling in your own
values:

```bash
# ---------------------------------------------------------------
# Network addresses
# ---------------------------------------------------------------
export CONTROLLER_IP=10.0.0.5        # OpenStack controller / RabbitMQ / Keystone / MariaDB
export MANILA_HOST_IP=10.0.0.10      # This Manila host's IP address
export WEKA_IP=10.0.1.50             # Weka cluster management IP or hostname
export WEKA_VERSION=4.2.0            # Weka cluster version (from Weka GUI → About)
export OS_REGION=RegionOne           # OpenStack region name

# ---------------------------------------------------------------
# Passwords  — choose strong values, do not reuse passwords
# ---------------------------------------------------------------
export MANILA_DB_PASS='Ch@ngeMe_DB1!'        # Manila MariaDB password
export MANILA_SVC_PASS='Ch@ngeMe_Svc1!'      # Manila Keystone service user password
export RABBIT_PASS='Ch@ngeMe_MQ1!'           # RabbitMQ 'openstack' user password
export WEKA_ADMIN_PASS='<your-weka-admin-password>'   # Existing Weka admin password
export WEKA_MANILA_PASS='Ch@ngeMe_Weka1!'    # New password for the manila-driver Weka user

# ---------------------------------------------------------------
# Share / test settings
# ---------------------------------------------------------------
export CLIENT_IP=192.168.10.5        # IP of the client that will mount shares
export TEST_SHARE_NAME=my-first-share
```

> **Tip:** Save this block to a file (e.g. `~/manila-weka-env.sh`) and
> `source ~/manila-weka-env.sh` at the start of each session to restore
> the variables.

---

## Step 0 — Set Up the Manila Host

This step installs OpenStack Manila on a dedicated Linux server and
registers it with your existing OpenStack environment.  Skip this step
if Manila is already running in your environment.

### 0a — Choose a server

The Manila host needs:

- 4 vCPUs / 8 GB RAM minimum (16 GB recommended for production)
- 50 GB root disk
- Network access to your OpenStack controller (Keystone, RabbitMQ,
  MariaDB) and to the Weka cluster (port 14000)
- The same OS as your other OpenStack nodes (consistency matters for
  package versions)

### 0b — Install OS packages

**RHEL 8 / Rocky Linux 8 / AlmaLinux 8:**

```bash
# Enable the RDO (Red Hat OpenStack Distribution) repository
sudo dnf install -y centos-release-openstack-antelope
sudo dnf update -y

# Install Manila packages
sudo dnf install -y openstack-manila \
                    openstack-manila-share \
                    python3-manilaclient \
                    python3-pymysql
```

**Ubuntu 22.04:**

```bash
# Enable the Ubuntu Cloud Archive for OpenStack Antelope
sudo add-apt-repository cloud-archive:antelope
sudo apt-get update

# Install Manila packages
sudo apt-get install -y python3-manila \
                        manila-api \
                        manila-scheduler \
                        manila-share \
                        python3-manilaclient \
                        python3-pymysql
```

### 0c — Create the Manila database

Run these commands on your **database host** (or on the Manila host if
MariaDB is local):

```bash
sudo mysql -u root -p -e "
CREATE DATABASE manila CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;
GRANT ALL PRIVILEGES ON manila.* TO 'manila'@'localhost'
  IDENTIFIED BY '${MANILA_DB_PASS}';
GRANT ALL PRIVILEGES ON manila.* TO 'manila'@'%'
  IDENTIFIED BY '${MANILA_DB_PASS}';
FLUSH PRIVILEGES;
"
```

### 0d — Register Manila with Keystone

Run these commands on any host where the OpenStack admin credentials are
loaded (usually the controller):

```bash
# Source admin credentials
source /etc/openstack/admin-openrc.sh   # adjust path to your env file

# Create the manila service user
openstack user create --domain default \
  --password "${MANILA_SVC_PASS}" manila

# Assign the admin role
openstack role add --project service --user manila admin

# Register the shared-filesystem service
openstack service create --name manila \
  --description "OpenStack Shared Filesystems" \
  "share"

openstack service create --name manilav2 \
  --description "OpenStack Shared Filesystems v2" \
  "sharev2"

# Create the API endpoints
for iface in public internal admin; do
  openstack endpoint create --region "${OS_REGION}" \
    share $iface "http://${MANILA_HOST_IP}:8786/v1/%(tenant_id)s"

  openstack endpoint create --region "${OS_REGION}" \
    sharev2 $iface "http://${MANILA_HOST_IP}:8786/v2"
done
```

### 0e — Configure manila.conf

The Manila configuration file lives at `/etc/manila/manila.conf`.  Back
it up, then write the base configuration:

```bash
sudo cp /etc/manila/manila.conf /etc/manila/manila.conf.orig

sudo tee /etc/manila/manila.conf > /dev/null <<EOF
[DEFAULT]
# Message queue (RabbitMQ)
transport_url = rabbit://openstack:${RABBIT_PASS}@${CONTROLLER_IP}:5672/

# Keystone auth for service-to-service calls
auth_strategy = keystone
my_ip = ${MANILA_HOST_IP}

# Logging
log_file = /var/log/manila/manila.log

[database]
connection = mysql+pymysql://manila:${MANILA_DB_PASS}@${CONTROLLER_IP}/manila

[keystone_authtoken]
www_authenticate_uri  = http://${CONTROLLER_IP}:5000
auth_url              = http://${CONTROLLER_IP}:5000
memcached_servers     = ${CONTROLLER_IP}:11211
auth_type             = password
project_domain_name   = Default
user_domain_name      = Default
project_name          = service
username              = manila
password              = ${MANILA_SVC_PASS}

[oslo_concurrency]
lock_path = /var/lib/manila/tmp
EOF
```

### 0f — Populate the database

```bash
sudo manila-manage db sync
```

You should see migration output ending with no errors.

### 0g — Start and enable Manila services

**RHEL / Rocky / AlmaLinux:**

```bash
sudo systemctl enable --now openstack-manila-api \
                              openstack-manila-scheduler \
                              openstack-manila-share
```

**Ubuntu:**

```bash
sudo systemctl enable --now manila-api \
                              manila-scheduler \
                              manila-share
```

### 0h — Verify Manila is running

From any host with the OpenStack client configured:

```bash
source /etc/openstack/admin-openrc.sh
openstack share service list
```

Expected output — all services should show `State: up`:

```
+----+------------------+----------+---------+-------+----------------------------+
| Id | Binary           | Host     | Zone    | State | Status                     |
+----+------------------+----------+---------+-------+----------------------------+
|  1 | manila-scheduler | manila   | nova    | up    | enabled                    |
|  2 | manila-share     | manila@weka | nova | up    | enabled                    |
+----+------------------+----------+---------+-------+----------------------------+
```

If any service shows `State: down`, check its log:

```bash
sudo journalctl -u openstack-manila-share -n 50
```

---

## Step 1 — Verify Network Connectivity

Before installing anything, confirm the Manila host can reach the Weka
cluster API.

Log in to the Manila host and run:

```bash
curl -k https://${WEKA_IP}:14000/api/v2/status
```

**Expected output** — you should see JSON similar to:

```json
{"data": {"name": "my-cluster", "release": "4.2.0", ...}}
```

**If you get "Connection refused" or "No route to host":**

- Check your firewall allows TCP port 14000 from the Manila host to the Weka cluster.
- On RHEL/Rocky: `firewall-cmd --list-all` on both sides
- On Ubuntu: `ufw status` on both sides
- Ask your network team to open port 14000 between the two hosts

Do not continue until this curl command succeeds.

---

## Step 2 — Install the WekaFS Kernel Module

The WekaFS POSIX client is a Linux kernel module that must be installed on
the Manila host.  This is what allows Manila to mount Weka filesystems
directly rather than going through NFS.

> **Kernel compatibility:** The WekaFS kernel module does not compile on
> Linux kernel **6.17 or later** due to a breaking change in the kernel's
> `inode_operations` struct.  Before installing, check your kernel version:
> ```bash
> uname -r
> ```
> If you are on kernel 6.17+, pin the kernel before proceeding:
> ```bash
> sudo apt-mark hold linux-aws linux-image-aws linux-headers-aws
> ```
> See [Known Issues](known-issues.md#1-wekafs-kernel-module-incompatible-with-linux-kernel-617)
> for full details.  If you cannot pin the kernel, use NFS protocol instead
> — skip this step and create `NFS` share types in Step 10.

### 2a — Download and install the Weka client package

The client package is downloaded from your Weka cluster itself.  This
ensures the client version matches your cluster exactly.

```bash
curl -k -o weka-client.tar \
  https://${WEKA_IP}:14000/dist/v1/install/${WEKA_VERSION}
tar xf weka-client.tar
sudo ./install.sh
```

> **Note:** The exact URL format may vary by Weka version.  If the above
> does not work, log in to the Weka GUI, click your username in the top
> right, then **Download Client**.

### 2b — Load the kernel module

```bash
sudo modprobe wekafsio
```

### 2c — Verify it loaded

```bash
lsmod | grep wekafsio
```

You should see output like:

```
wekafs               1234567  0
```

If the output is empty, the module did not load.  Check the kernel log for
errors:

```bash
sudo dmesg | tail -20
```

### 2d — Make it load automatically at boot

```bash
echo "wekafs" | sudo tee /etc/modules-load.d/wekafs.conf
```

Verify it will survive a reboot:

```bash
cat /etc/modules-load.d/wekafs.conf
# should output: wekafs
```

---

## Step 3 — Create a Dedicated API User in Weka

You should create a dedicated Weka user for Manila rather than using the
`admin` account.  This limits the blast radius if credentials are
accidentally exposed.

### 3a — Log in to the Weka cluster CLI

```bash
weka user login admin "${WEKA_ADMIN_PASS}" --hostname "${WEKA_IP}"
```

### 3b — Create the Manila user

```bash
weka user add manila-driver \
  --password "${WEKA_MANILA_PASS}" \
  --role OrgAdmin
```

> **Role note:** `OrgAdmin` gives permission to create and manage
> filesystems.  If you prefer a more restricted role, `CSAdmin` also works.

### 3c — Test the new credentials

```bash
curl -k -X POST https://${WEKA_IP}:14000/api/v2/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"manila-driver\",\"password\":\"${WEKA_MANILA_PASS}\",\"org\":\"Root\"}"
```

You should receive a response containing `"access_token"`.  If you see
`"error"`, double-check the username, password, and org name.

---

## Step 4 — Install the Driver Package

All commands in this step run on the **Manila host**.

### 4a — Activate the Manila Python environment

Manila typically runs in a virtual environment or a system Python.  Find
the right Python first:

```bash
# Try these in order until one works:
which manila-manage
ls /opt/stack/manila/           # DevStack installations
ls /usr/lib/python3/dist-packages/manila/   # package installations
```

For a typical production install (e.g. from RDO or Ubuntu Cloud Archive):

```bash
sudo pip3 install manila-weka-driver
```

For a DevStack or virtualenv install:

```bash
# Activate Manila's virtualenv first
source /opt/stack/manila/.venv/bin/activate    # adjust path as needed
pip install manila-weka-driver
```

### 4b — Verify the driver installed

```bash
python3 -c "from manila.share.drivers.weka.driver import WekaShareDriver; print('OK')"
```

You should see `OK`.  If you get `ModuleNotFoundError`, the driver is not
on the Python path — make sure you installed into the same Python that
Manila uses.

---

## Step 5 — Create the Mount Point Directory

The driver needs a directory on the Manila host where it will mount Weka
filesystems.

```bash
sudo mkdir -p /mnt/weka
sudo chown manila:manila /mnt/weka   # or whatever user Manila runs as
```

Check which user runs the Manila share service:

```bash
ps aux | grep manila-share | head -3
# Look at the first column (username)
```

---

## Step 6 — Edit manila.conf

`manila.conf` is the main OpenStack Manila configuration file.  Its usual
location is `/etc/manila/manila.conf`.

### 6a — Back up the original

```bash
sudo cp /etc/manila/manila.conf \
  /etc/manila/manila.conf.backup-$(date +%Y%m%d)
```

### 6b — Add the Weka backend stanza

First, add `weka` to the `enabled_share_backends` list in `[DEFAULT]`:

```bash
sudo sed -i '/^\[DEFAULT\]/a enabled_share_backends = weka' \
  /etc/manila/manila.conf
```

> **If you already have other backends**, edit the line manually instead:
> `enabled_share_backends = ceph,nfs,weka`

Then append the Weka backend section to the end of the file:

```bash
sudo tee -a /etc/manila/manila.conf > /dev/null <<EOF

[weka]
# Driver class — do not change this line
share_driver = manila.share.drivers.weka.driver:WekaShareDriver

# Human-readable name shown in "openstack share pool list"
share_backend_name = weka

# This driver manages its own networking — always false for Weka
driver_handles_share_servers = false

# Feature flags
snapshot_support = true
create_share_from_snapshot_support = true
revert_to_snapshot_support = true

# Connection
weka_api_server = ${WEKA_IP}
weka_api_port   = 14000
weka_ssl_verify = true

# Authentication
weka_username     = manila-driver
weka_password     = ${WEKA_MANILA_PASS}
weka_organization = Root

# Filesystem settings
weka_filesystem_group  = default
weka_share_name_prefix = manila_

# POSIX client on this Manila host
weka_mount_point_base = /mnt/weka
weka_num_cores        = 1
EOF
```

### 6c — Validate the config file syntax

```bash
manila-manage config list 2>&1 | grep -i error
```

If this command outputs no errors (or does not exist on your version), the
syntax is fine.

---

## Step 7 — Restart the Manila Share Service

```bash
# systemd (most production systems)
sudo systemctl restart openstack-manila-share
sudo systemctl status  openstack-manila-share

# DevStack
sudo systemctl restart devstack@m-shr
sudo systemctl status  devstack@m-shr
```

**What to look for in the status output:**

```
Active: active (running) since ...
```

If it shows `failed`, check the logs immediately (Step 8 below).

---

## Step 8 — Check the Logs

After restarting, tail the Manila share service log to look for errors:

```bash
# Most common log locations:
sudo journalctl -u openstack-manila-share -f   # systemd journal
sudo tail -f /var/log/manila/manila-share.log   # file-based logging
```

**Good signs** — you should see lines like:

```
INFO manila.share.drivers.weka.driver WekaShareDriver 1.0.0 connected
  to cluster 'my-cluster' (Weka version 4.2.0)
```

**Bad signs** — common error messages and what they mean:

| Error message | Likely cause | Fix |
|---------------|-------------|-----|
| `WekaAuthError: authentication failed` | Wrong username/password/org | Re-check Step 3c |
| `ConnectionRefusedError` | Wrong IP or port | Re-check Step 1 |
| `WekaConfigurationError: weka_api_server not set` | Missing config option | Re-check Step 6b |
| `WekaFS kernel module not found` | Module not loaded | Re-run Step 2b |
| `ModuleNotFoundError: manila.share.drivers.weka` | Driver not installed | Re-run Step 4 |

---

## Step 9 — Verify the Backend is Registered

From any host with the OpenStack client configured:

```bash
openstack share pool list --detail
```

You should see your Weka backend listed:

```
+----------------------------------+------+-------+------------------+
| Name                             | Host | Total | Free             |
+----------------------------------+------+-------+------------------+
| controller@weka#weka             | ...  | 100.0 | 70.0             |
+----------------------------------+------+-------+------------------+
```

Also check:

```bash
openstack share service list
```

The `manila-share` service for the Weka backend should show `State: up`
and `Status: enabled`.

If the backend does not appear after 2 minutes, check the logs (Step 8).

---

## Step 10 — Create a Share Type

A **share type** tells Manila which backend to use when a user creates a
share.  You need to create one for Weka.

```bash
openstack share type create \
  weka-default \
  false \
  --extra-specs driver_handles_share_servers=false \
                share_backend_name=weka
```

Verify it was created:

```bash
openstack share type list
```

You should see `weka-default` in the list.

---

## Step 11 — Create Your First Share

Now test the whole stack end-to-end by creating a share.

### 11a — Create a 10 GiB WEKAFS share

```bash
openstack share create \
  --name "${TEST_SHARE_NAME}" \
  --share-type weka-default \
  --size 10 \
  WEKAFS
```

### 11b — Wait for it to become available

```bash
openstack share show "${TEST_SHARE_NAME}"
```

Watch the `Status` field.  It will go:

```
creating  →  available
```

This usually takes 10–30 seconds.  If it goes to `error`, check the logs.

### 11c — Allow access from a client

```bash
openstack share access create \
  "${TEST_SHARE_NAME}" \
  ip \
  "${CLIENT_IP}" \
  --access-level rw
```

Check the access rule was applied:

```bash
openstack share access list "${TEST_SHARE_NAME}"
```

The `State` should show `active`.

### 11d — Get the export path

```bash
openstack share show "${TEST_SHARE_NAME}" -c export_locations
```

You will see output like:

```
+------------------+----------------------------------------------------+
| export_locations | path = 10.0.1.50/manila_<uuid>                     |
+------------------+----------------------------------------------------+
```

Save the path to a variable for use in the next step:

```bash
export SHARE_PATH=$(openstack share show "${TEST_SHARE_NAME}" \
  -c export_locations -f value | grep -oP '(?<=path = )\S+')
echo "Share path: ${SHARE_PATH}"
```

---

## Step 12 — Mount the Share on a Client

On a client machine that has the WekaFS client installed (same Steps 2a–2d):

```bash
# Create a mount point
mkdir -p /mnt/${TEST_SHARE_NAME}

# Mount the share
mount -t wekafs "${SHARE_PATH}" /mnt/${TEST_SHARE_NAME}

# Verify it's mounted
df -h /mnt/${TEST_SHARE_NAME}

# Write a test file
echo "Hello from Manila Weka!" > /mnt/${TEST_SHARE_NAME}/test.txt
cat /mnt/${TEST_SHARE_NAME}/test.txt
```

If the mount succeeds and you can write a file, the deployment is complete.

### Mounting via NFS instead

If the client cannot install the WekaFS kernel module, use NFS:

```bash
# Create an NFS share type
openstack share type create \
  weka-nfs false \
  --extra-specs driver_handles_share_servers=false share_backend_name=weka

# Create an NFS share
openstack share create \
  --name my-nfs-share \
  --share-type weka-nfs \
  --size 10 \
  NFS

# Get the NFS export path
export NFS_PATH=$(openstack share show my-nfs-share \
  -c export_locations -f value | grep -oP '(?<=path = )\S+')

# Mount with standard NFS
mkdir -p /mnt/my-nfs-share
mount -t nfs "${NFS_PATH}" /mnt/my-nfs-share
```

---

## Step 13 — Persistent Mounts (Optional)

To mount shares automatically at boot, add an entry to `/etc/fstab` on
each client:

```bash
# WekaFS share — append to /etc/fstab
echo "${SHARE_PATH}  /mnt/${TEST_SHARE_NAME}  wekafs  defaults,num_cores=1  0  0" \
  | sudo tee -a /etc/fstab

# NFS share — append to /etc/fstab
echo "${NFS_PATH}  /mnt/my-nfs-share  nfs  defaults,_netdev  0  0" \
  | sudo tee -a /etc/fstab
```

Test the fstab entries without rebooting:

```bash
mount -a
df -h | grep mnt
```

---

## Verification Checklist

Use this checklist to confirm every step completed successfully:

- [ ] Manila packages installed (`manila-manage db sync` completed)
- [ ] Manila service user exists in Keystone (`openstack user list | grep manila`)
- [ ] Manila API endpoints registered (`openstack endpoint list | grep share`)
- [ ] All Manila services show `State: up` in `openstack share service list`
- [ ] `curl -k https://${WEKA_IP}:14000/api/v2/status` returns JSON
- [ ] `lsmod | grep wekafs` shows the module is loaded
- [ ] `python3 -c "from manila.share.drivers.weka.driver import WekaShareDriver"` prints nothing (no error)
- [ ] `/mnt/weka` directory exists and is owned by the Manila user
- [ ] `manila.conf` has a `[weka]` section with correct credentials
- [ ] `systemctl status openstack-manila-share` shows `active (running)`
- [ ] Manila log shows `WekaShareDriver ... connected to cluster`
- [ ] `openstack share pool list` shows the Weka backend
- [ ] `openstack share service list` shows `State: up`
- [ ] A test share reaches `available` status
- [ ] An access rule reaches `active` state
- [ ] You can mount and write to the share from a client

---

## Uninstalling the Driver

If you need to remove the driver:

### 1 — Remove the backend from manila.conf

Edit `/etc/manila/manila.conf` and remove `weka` from
`enabled_share_backends`, then delete the entire `[weka]` section.

### 2 — Restart Manila

```bash
sudo systemctl restart openstack-manila-share
```

### 3 — Delete existing shares (optional)

Any existing Weka shares will still exist on the cluster but will no
longer be managed by Manila.  Delete them before removing the driver if
you want a clean teardown:

```bash
# List all Weka shares
openstack share list --share-type weka-default

# Delete each share
openstack share delete <share-id>
```

### 4 — Remove the package

```bash
pip3 uninstall manila-weka-driver
```

---

## Getting Help

If you encounter a problem not covered in this guide:

1. Check the [Troubleshooting section in README.md](../README.md#troubleshooting)
2. Collect the Manila share service log:
   ```bash
   sudo journalctl -u openstack-manila-share --since "1 hour ago" > manila-share.log
   ```
3. Open a GitHub issue at `https://github.com/weka/manila-weka-driver/issues`
   and attach the log (remove any passwords first).
