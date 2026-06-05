Vagrant.configure("2") do |config|

  config.vm.box = "generic/ubuntu2204"

  # =========================
  # VM1
  # =========================
  config.vm.define "vm1" do |vm1|
    vm1.vm.hostname = "research-node-1"

    vm1.vm.network "private_network",
      ip: "192.168.56.11"

    vm1.vm.network "private_network",
      ip: "192.168.121.11"

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

    vm2.vm.network "private_network",
      ip: "192.168.56.12"

    vm2.vm.network "private_network",
      ip: "192.168.121.12"

    vm2.vm.provider :libvirt do |lv|
      lv.memory = 2048
      lv.cpus = 2
      lv.cpu_mode = "host-passthrough"
    end
  end

end
