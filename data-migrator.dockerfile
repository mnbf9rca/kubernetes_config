# Basic data migration container
# Copies data from /source to /target then exits
# v1, 2022 github.com/mnbf9rca
# hub.docker.com/r/mnbf9rca/data-migrator
# docker build --tag=mnbf9rca/data-migrator:<> -t=mnbf9rca/data-migrator:latest  -f ./data-migrator.dockerfile .  
# 
FROM phusion/baseimage:jammy-1.0.1

RUN apt-get update && apt-get upgrade -y -o Dpkg::Options::="--force-confold"

# ...put your own build instructions here...
run install_clean	rsync

# Clean up APT when done.
RUN apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# expect /source and /target locations
VOLUME ["/source", "/target"]

# Use baseimage-docker's init system to run the migration
RUN mkdir -p /etc/my_init.d
COPY data-migrator-script.sh /etc/my_init.d/data-migrator-script.sh
RUN chmod +x /etc/my_init.d/data-migrator-script.sh

# keep container alive
RUN mkdir -p /etc/service/keepalive
COPY data-migrator-keepalive.sh /etc/service/keepalive/run
RUN chmod +x /etc/service/keepalive/run

# use baseimage-docker's init system
CMD ["/sbin/my_init"]

