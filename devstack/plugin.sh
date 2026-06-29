#!/usr/bin/env bash
# =============================================================================
# DevStack plugin for the Manila Weka share driver
#
# INSTALLATION: This file (and devstack/settings) must be present in the
# weka/manila-weka-driver GitHub repository under the devstack/ directory
# for the enable_plugin directive in local.conf to work.
#
# Usage in local.conf:
#   enable_plugin manila-weka-driver \
#     git@github.com:weka/manila-weka-driver.git main
#
# This plugin:
#   1. Installs driver dependencies and symlinks the driver into Manila's
#      source tree (pip install of the package itself is avoided because the
#      namespace package layout conflicts with Manila's own manila.share)
#   2. Ensures the WekaFS kernel module is available
#   3. Creates the /mnt/weka mount base directory
# =============================================================================

MANILA_WEKA_DRIVER_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Source DevStack functions if not already loaded
if ! type is_service_enabled &>/dev/null; then
    source "${TOP_DIR}/functions"
fi

# ─── Installation phase ────────────────────────────────────────────────────────

function install_manila_weka_driver {
    echo_summary "Installing Manila Weka driver"

    # We do NOT pip-install the manila-weka-driver package itself because its
    # namespace package layout (manila.share.drivers.weka) creates implicit
    # namespace entries for manila/, manila/share/, and manila/share/drivers/
    # that shadow Manila's own modules (e.g. manila.share.share_types).
    # All driver dependencies (requests, oslo.*) are already provided by Manila,
    # so we only need to symlink the driver into Manila's source tree.
    local MANILA_DRIVERS_DIR="${MANILA_DIR}/manila/share/drivers"
    local WEKA_DRIVER_SRC="${MANILA_WEKA_DRIVER_DIR}/manila/share/drivers/weka"
    local WEKA_DRIVER_DEST="${MANILA_DRIVERS_DIR}/weka"

    if [ -d "${MANILA_DRIVERS_DIR}" ] && [ -d "${WEKA_DRIVER_SRC}" ]; then
        if [ ! -e "${WEKA_DRIVER_DEST}" ]; then
            ln -sfn "${WEKA_DRIVER_SRC}" "${WEKA_DRIVER_DEST}"
            echo "Linked Weka driver into Manila source tree: ${WEKA_DRIVER_DEST}"
        else
            echo "Weka driver already present at ${WEKA_DRIVER_DEST}"
        fi
    else
        echo "WARNING: Manila drivers dir (${MANILA_DRIVERS_DIR}) or driver source (${WEKA_DRIVER_SRC}) not found."
        echo "The driver may not be importable by Manila. Check your installation."
    fi

    # Symlink manila/privsep/weka.py — imported by driver.py and posix.py as
    # `from manila.privsep import weka as weka_privsep`. Manila provides
    # privsep/__init__.py itself; we only add the weka module.
    local WEKA_PRIVSEP_SRC="${MANILA_WEKA_DRIVER_DIR}/manila/privsep/weka.py"
    local WEKA_PRIVSEP_DEST="${MANILA_DIR}/manila/privsep/weka.py"

    if [ -f "${WEKA_PRIVSEP_SRC}" ] && [ -d "${MANILA_DIR}/manila/privsep" ]; then
        if [ ! -e "${WEKA_PRIVSEP_DEST}" ]; then
            ln -sfn "${WEKA_PRIVSEP_SRC}" "${WEKA_PRIVSEP_DEST}"
            echo "Linked Weka privsep module into Manila source tree: ${WEKA_PRIVSEP_DEST}"
        else
            echo "Weka privsep module already present at ${WEKA_PRIVSEP_DEST}"
        fi
    else
        echo "WARNING: Manila privsep dir (${MANILA_DIR}/manila/privsep) or privsep source (${WEKA_PRIVSEP_SRC}) not found."
        echo "The driver may not be importable by Manila. Check your installation."
    fi
}

# ─── Configuration phase ───────────────────────────────────────────────────────

function configure_manila_weka_driver {
    echo_summary "Configuring Manila Weka driver"

    # Ensure WekaFS kernel module is loaded
    # This is required for the POSIX client (WekaFS mounts) to work.
    # The Weka client installs the module as 'wekafsio' (wekafsgw loads automatically).
    sudo depmod -a 2>/dev/null || true
    if grep -q "wekafs" /proc/filesystems 2>/dev/null; then
        echo "wekafs is already registered in /proc/filesystems"
    elif modprobe wekafsio 2>/dev/null; then
        echo "wekafs kernel module loaded successfully"
        # Persist across reboots
        echo "wekafsio" | sudo tee /etc/modules-load.d/wekafs.conf > /dev/null
    else
        echo "WARNING: Could not load wekafsio kernel module."
        echo "  The Weka agent may not be installed, or this kernel version is unsupported."
        echo "  WekaFS POSIX access will not work until the module is loaded."
        echo "  NFS protocol access may still be available if configured."
    fi

    # Create and permission the WekaFS mount base directory
    local WEKA_MOUNT_BASE="${MANILA_OPTGROUP_weka_weka_mount_point_base:-/mnt/weka}"
    sudo mkdir -p "${WEKA_MOUNT_BASE}"
    sudo chmod 777 "${WEKA_MOUNT_BASE}"
    echo "Created WekaFS mount base: ${WEKA_MOUNT_BASE}"

    # Patch Manila constants.py to add WEKAFS to SUPPORTED_SHARE_PROTOCOLS.
    # Manila 2024.2 does not include WEKAFS in its hardcoded protocol list.
    local CONSTANTS_FILE="${MANILA_DIR}/manila/common/constants.py"
    if [ -f "${CONSTANTS_FILE}" ]; then
        if grep -q "'WEKAFS'" "${CONSTANTS_FILE}"; then
            echo "WEKAFS already present in Manila SUPPORTED_SHARE_PROTOCOLS"
        else
            sudo sed -i "s/SUPPORTED_SHARE_PROTOCOLS = (/SUPPORTED_SHARE_PROTOCOLS = (/;s/'MAPRFS')/'MAPRFS', 'WEKAFS')/" \
                "${CONSTANTS_FILE}"
            echo "Patched Manila constants.py to add WEKAFS to SUPPORTED_SHARE_PROTOCOLS"
        fi
    else
        echo "WARNING: Manila constants.py not found at ${CONSTANTS_FILE}"
    fi
}

# ─── Plugin dispatcher ─────────────────────────────────────────────────────────

if is_service_enabled manila; then
    if [[ "$1" == "stack" && "$2" == "pre-install" ]]; then
        : # Nothing needed in pre-install for this driver

    elif [[ "$1" == "stack" && "$2" == "install" ]]; then
        install_manila_weka_driver

    elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
        configure_manila_weka_driver

    elif [[ "$1" == "stack" && "$2" == "extra" ]]; then
        : # Nothing needed in extra for this driver

    elif [[ "$1" == "unstack" ]]; then
        : # Unmount any active WekaFS mounts
        if mountpoint -q /mnt/weka 2>/dev/null; then
            sudo umount -l /mnt/weka 2>/dev/null || true
        fi

    elif [[ "$1" == "clean" ]]; then
        sudo rm -rf /mnt/weka
    fi
fi
