
# How to implement
## basic steps
1. install ubuntu server 
	1. install basic config:
        ```
        wget https://raw.githubusercontent.com/mnbf9rca/public_server_config/master/basic_config.sh
        sudo chmod +x ./basic_config.sh
        sudo ./basic_config.sh
        ```
	1. increase max file limit:
		1. GUI login:
			 - modify `/etc/systemd/user.conf` by setting `DefaultLimitNOFILE=1048576`
			 - modify `/etc/systemd/system.conf` by setting `DefaultLimitNOFILE=2097152`
		1. non-GUI login:
			 modify `/etc/security/limits.conf` to add at the end:
			```
			* hard nofile 2097152
			* soft nofile 2097152
			root soft nofile 64000
			root hard nofile 64000
			```
		4. reboot
    1. Install Docker CE https://docs.docker.com/install/linux/docker-ce/ubuntu/

# create cluster, basic config
1. in Rancher, create new custom cluster
1. edit config map nginx-cofiguration by pasting this in to the "edit" box (it'll parse it to individual values):
  ```
  hsts: "true"
  hsts-include-subdomains: "true"
  hsts-max-age: "0"
  hsts-preload: "false"
  proxy-body-size: "20m"
  ```
1. Create new "project" called "Downloads"

## apps
> In general, you just paste the yaml in to the `Import YAML` box
1. Create persistent volume claims and `downloads` namespace with `persistent-nfs-storage.yaml`
1. sabnzbd, couchpotato, sonarr - just create a cname sab, cp, sonarr.* and deploy the relevant yaml file.
1. emby --> still working on this
	1. deploy emby.yaml
    1. edit tcp_services configmap to add emby

```
