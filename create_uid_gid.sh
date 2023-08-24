#!/bin/bash

# Default UID and GID
DEFAULT_USER_ID=1999
DEFAULT_USER_GID=1999

# Take UID and GID from command-line arguments if provided
USER_ID=${1:-$DEFAULT_USER_ID}
USER_GID=${2:-$DEFAULT_USER_GID}

# Function to check if UID or GID already exists
check_id() {
  if grep -q "^[^:]*:[^:]*:$1:" /etc/passwd /etc/group; then
    echo "Error: ID $1 is already in use."
    exit 1
  fi
}

# Check if UID and GID are already in use
check_id $USER_ID
check_id $USER_GID

# Confirmation message
read -p "Are you sure you want to create user 'containeruser' with UID $USER_ID and group 'dataaccessgroup' with GID $USER_GID? [y/N] " response
if [[ $response =~ ^([yY][eE][sS]|[yY])$ ]]; then
  # Create group
  sudo groupadd -g $USER_GID dataaccessgroup
  if [ $? -ne 0 ]; then
    echo "Failed to create group."
    exit 1
  fi

  # Create user
  sudo useradd -u $USER_ID -g $USER_GID -s /usr/sbin/nologin containeruser
  if [ $? -ne 0 ]; then
    echo "Failed to create user."
    exit 1
  fi

  echo "User 'containeruser' and group 'dataaccessgroup' created successfully."
else
  echo "Operation cancelled by user."
fi
