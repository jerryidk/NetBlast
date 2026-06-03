Vagrant.configure("2") do |config|

  config.vm.box = "generic/ubuntu2204"

  # =========================
  # VM1
  # =========================
  config.vm.define "vm1" do |vm1|
    vm1.vm.hostname = "research-node-1"
		vm1.vm.synced_folder "./maglevl2fwd/", "/home/vagrant/maglevl2fwd"
    # NIC #1 (Private Network A)
    vm1.vm.network "private_network",
      ip: "192.168.56.11",
      libvirt__forward_mode: "none"

    # NIC #2 (Private Network B)
    vm1.vm.network "private_network",
      ip: "192.168.121.11",
      libvirt__forward_mode: "none"

    vm1.vm.provider :libvirt do |lv|
      lv.memory = 2048
      lv.cpus = 2
      lv.cpu_mode = "host-passthrough"
    end
  end

  # =========================
  # VM2
  # =========================
  config.vm.define "vm2" do |vm2|
    vm2.vm.hostname = "research-node-2"

    # NIC #1
    vm2.vm.network "private_network",
      ip: "192.168.56.12"

    vm2.vm.provider :libvirt do |lv|
      lv.memory = 2048
      lv.cpus = 1
      lv.cpu_mode = "host-passthrough"
    end
  end

end
