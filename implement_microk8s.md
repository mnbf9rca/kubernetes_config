## basic steps
1. install ubuntu server 
	1. install config from other repo
	1. remove splash screen so you can see boot and shutdown messages:
		- modify  `/etc/default/grub` to remove `quiet` and `splash` from `GRUB_CMDLINE_LINUX_DEFAULT`:
			```
			GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
			```

		- Run  `sudo update-grub`
	1. update `sudo apt-get dist-upgrade --autoremove --no-install-recommends --assume-yes`
	2. increase max file limit:
		1. GUI login:
			 - modify `/etc/systemd/user.conf` by setting `DefaultLimitNOFILE=1048576`
			 - modify `/etc/systemd/system.conf` by setting `DefaultLimitNOFILE=2097152`
		 2. non-GUI login:
			 modify `/etc/security/limits.conf` to add at the end:
			```
			* hard nofile 2097152
			* soft nofile 2097152
			root soft nofile 64000
			root hard nofile 64000
			```
		3. install open-vm-tools with apt
		4. reboot
	3. apply the [microk8s](https://microk8s.io/) snap e.g. https://snapcraft.io/microk8s says:
		```
		sudo snap install microk8s --classic
		```	
	4. open ufw for kube:
		`sudo ufw allow in on cbr0 && sudo ufw allow out on cbr0`
	6. enable ssh, http and https on ufw:
		```
		sudo ufw allow ssh
		sudo ufw allow http
		sudo ufw allow https
		sudo ufw enable
		sudo ufw reload
		```
	6. install iptables-persistent and configure
	    ```
	    sudo iptables -P FORWARD ACCEPT
	    sudo apt-get install iptables-persistent
	    ```
	    
2. switch kubectl to use nano:
	```
	export KUBE_EDITOR="nano"
	echo export KUBE_EDITOR="nano" >> ~/.bashrc
	```
2. enable DNS, dashboard, ingress and make kubectl work without microk8s in front. Enable DNS first because of https://github.com/ubuntu/microk8s/issues/706
	```
	sudo microk8s.enable dns 
	sudo snap alias microk8s.kubectl kubectl
	```
## Enable Kube ingress
1. enable ingress:
	`sudo microk8s.enable ingress`
3. apply `nginx-load-balancer-microk8s-conf.yaml` (or at least the HSTS part) to disable HSTS, and set max file upload size to accomodate large files (e.g. nzbs)
3. edit `spec`:`containers`:`args` section of `nginx-ingress-microk8s-controller` to add `--enable-ssl-passthrough` option (see [documentation](https://github.com/kubernetes/ingress-nginx/blob/master/docs/user-guide/cli-arguments.md))
    - `kubectl --namespace=ingress edit DaemonSet/nginx-ingress-microk8s-controller` - remember to indent with spaces not tabs.
    
## dashboard
1. enable dashboard:
	`sudo microk8s.enable dashboard`
6. create DNS entry for dashboard (e.g. `k-dashboard` as cname for `k`)
7. Publish dashboard as an ingress
	1. edit existing service to switch from `ClusterIP` to `NodePort`: `kubectl -n kube-system edit service kubernetes-dashboard`
	2. create [ingress-dashboard.yaml](ingress-dashboard.yaml) via ingress using `kubectl create -f ingress-dashboard.yaml`
1. Edit `kubernetes-dashboard` to add `--enable-skip-login` to the `spec`:`template`:`spec`:`containers`:`args` node - see [documentation](https://github.com/kubernetes/dashboard/blob/master/docs/common/dashboard-arguments.md)
    -  `kubectl --namespace=kube-system edit deployments.apps kubernetes-dashboard` 

8. (optional if not skip login) Create an admin user (from [here](https://github.com/kubernetes/dashboard/wiki/Creating-sample-user)) using [create-admin-user.yaml](create-admin-user.yaml) - not sure if this is strictly necessary as any user with `admin-user` role will work (or just "skip" login), but i did it.
9. Log in to the dashboard. Find the token using the command `kubectl -n kube-system describe secret $(kubectl -n kube-system get secret | grep admin-user | awk '{print $1}')`

## apps
1. Install NFS helper `sudo apt install nfs-common`
1. Create persistent volume claims and `downloads` namespace with `persistent-nfs-storage.yaml`
1. emby
	1. deploy emby.yaml
	2. that creates a port forward using a configmap called`ingress-tcp-passthrough` in line with [documentation](https://github.com/kubernetes/ingress-nginx/blob/master/docs/user-guide/exposing-tcp-udp-services.md).
	3. Configure the `nginx-ingress-microk8s-controller` daemon set to pass the port by adding `--tcp-services-configmap=$(POD_NAMESPACE)/ingress-tcp-passthrough` to the daemon definition
	4. open firewall: `sudo ufw allow 8086`
1. sabnzbd, couchpotato, sonarr - just create a cname sab, cp, sonarr.* and deploy the relevant yaml file.

## Helm
1. `microk8s.enable helm`
1. because of https://github.com/ubuntu/microk8s/issues/70 use `microk8s.helm init --override spec.selector.matchLabels.'name'='tiller',spec.selector.matchLabels.'app'='helm' --output yaml | sed 's@apiVersion: extensions/v1beta1@apiVersion: apps/v1@' | microk8s.kubectl apply -f -`


# to disable or enable snap at boot
from https://docs.snapcraft.io/service-management/3965
To prevent a service from starting on the next boot, use the  `--disable`  option:
```
$ sudo snap stop --disable lxd.daemon
```
The  _start_  command includes an  `--enable`  option to re-enable the automatic starting of a service when the system boots:
```
$ sudo snap start --enable lxd.daemon
```
