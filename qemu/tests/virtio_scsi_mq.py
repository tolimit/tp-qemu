import logging
import re

from autotest.client.shared import error
from autotest.client import local_host

from virttest import utils_misc
from virttest import env_process
from virttest import qemu_qtree


@error.context_aware
def run(test, params, env):
    """
    Qemu multiqueue test for virtio-scsi controller:

    1) Boot up a guest with virtio-scsi device which support multi-queue and
       the vcpu and images number of guest should match the multi-queue number
    2) Check the multi queue option from monitor
    3) Check device init status in guest
    4) Load I/O in all targets
    5) Check the interrupt queues in guest

    :param test: QEMU test object
    :param params: Dictionary with the test parameters
    :param env: Dictionary with test environment.
    """
    def proc_interrupts_results(results):
        results_dict = {}
        cpu_count = 0
        cpu_list = []
        for line in results.splitlines():
            line = line.strip()
            if re.match("CPU0", line):
                cpu_list = re.findall("CPU\d+", line)
                cpu_count = len(cpu_list)
                continue
            if cpu_count > 0:
                irq_key = re.split(":", line)[0]
                results_dict[irq_key] = {}
                content = line[len(irq_key) + 1:].strip()
                if len(re.split("\s+", content)) < cpu_count:
                    continue
                count = 0
                irq_des = ""
                for irq_item in re.split("\s+", content):
                    if count < cpu_count:
                        if count == 0:
                            results_dict[irq_key]["count"] = []
                        results_dict[irq_key]["count"].append(irq_item)
                    else:
                        irq_des += " %s" % irq_item
                    count += 1
                results_dict[irq_key]["irq_des"] = irq_des.strip()
        return results_dict, cpu_list

    timeout = float(params.get("login_timeout", 240))
    host_cpu_num = local_host.LocalHost().get_num_cpu()
    while host_cpu_num:
        num_queues = str(host_cpu_num)
        host_cpu_num &= host_cpu_num - 1
    params['smp'] = num_queues
    params['num_queues'] = num_queues
    images_num = int(num_queues)
    extra_image_size = params.get("image_size_extra_images", "512M")
    system_image = params.get("images")
    system_image_drive_format = params.get("system_image_drive_format", "ide")
    params["drive_format_%s" % system_image] = system_image_drive_format
    dev_type = params.get("dev_type", "i440FX-pcihost")

    error.context("Boot up guest with block devcie with num_queues"
                  " is %s and smp is %s" % (num_queues, params['smp']),
                  logging.info)
    for vm in env.get_all_vms():
        if vm.is_alive():
            vm.destroy()
    for extra_image in range(images_num):
        image_tag = "stg%s" % extra_image
        params["images"] += " %s" % image_tag
        params["image_name_%s" % image_tag] = "images/%s" % image_tag
        params["image_size_%s" % image_tag] = extra_image_size
        params["force_create_image_%s" % image_tag] = "yes"
        image_params = params.object_params(image_tag)
        env_process.preprocess_image(test, image_params, image_tag)

    params["start_vm"] = "yes"
    vm = env.get_vm(params["main_vm"])
    env_process.preprocess_vm(test, params, env, vm.name)
    session = vm.wait_for_login(timeout=timeout)

    error.context("Check irqbalance service status", logging.info)
    output = session.cmd_output("systemctl status irqbalance")
    if not re.findall("Active: active", output):
        session.cmd("systemctl start irqbalance")
        output = session.cmd_output("systemctl status irqbalance")
        output = utils_misc.strip_console_codes(output)
        if not re.findall("Active: active", output):
            raise error.TestNAError("Can not start irqbalance inside guest. "
                                    "Skip this test.")

    error.context("Pin vcpus to host cpus", logging.info)
    host_numa_nodes = utils_misc.NumaInfo()
    vcpu_num = 0
    for numa_node_id in host_numa_nodes.nodes:
        numa_node = host_numa_nodes.nodes[numa_node_id]
        for _ in range(len(numa_node.cpus)):
            if vcpu_num >= len(vm.vcpu_threads):
                break
            vcpu_tid = vm.vcpu_threads[vcpu_num]
            logging.debug("pin vcpu thread(%s) to cpu"
                          "(%s)" % (vcpu_tid,
                                    numa_node.pin_cpu(vcpu_tid)))
            vcpu_num += 1

    error.context("Verify num_queues from monitor", logging.info)
    qtree = qemu_qtree.QtreeContainer()
    try:
        qtree.parse_info_qtree(vm.monitor.info('qtree'))
    except AttributeError:
        raise error.TestNAError("Monitor deson't supoort qtree "
                                "skip this test")
    error_msg = "Number of queues mismatch: expect %s"
    error_msg += " report from monitor: %s(%s)"
    scsi_bus_addr = ""
    for qdev in qtree.get_qtree().get_children():
        if qdev.qtree["type"] == dev_type:
            for pci_bus in qdev.get_children():
                for pcic in pci_bus.get_children():
                    if pcic.qtree["class_name"] == "SCSI controller":
                        qtree_queues = pcic.qtree["num_queues"].split("(")[0]
                        if qtree_queues.strip() != num_queues.strip():
                            error_msg = error_msg % (num_queues,
                                                     qtree_queues,
                                                     pcic.qtree["num_queues"])
                            raise error.TestFail(error_msg)
                    if pcic.qtree["class_name"] == "SCSI controller":
                        scsi_bus_addr = pcic.qtree['addr']
                        break

    if not scsi_bus_addr:
        raise error.TestError("Didn't find addr from qtree. Please check "
                              "the log.")
    error.context("Check device init status in guest", logging.info)
    init_check_cmd = params.get("init_check_cmd", "dmesg | grep irq")
    output = session.cmd_output(init_check_cmd)
    irqs_pattern = params.get("irqs_pattern", "%s:\s+irq\s+(\d+)")
    irqs_pattern = irqs_pattern % scsi_bus_addr
    irqs_watch = re.findall(irqs_pattern, output)
    # As there are several interrupts count for virtio device:
    # config, control, event and request. And the each queue have
    # a request count. So the totally count for virtio device should
    # equal to queus number plus three.
    if len(irqs_watch) != 3 + int(num_queues):
        raise error.TestFail("Failed to check the interrupt ids from dmesg")
    irq_check_cmd = params.get("irq_check_cmd", "cat /proc/interrupts")
    output = session.cmd_output(irq_check_cmd)
    irq_results, _ = proc_interrupts_results(output)
    for irq_watch in irqs_watch:
        if irq_watch not in irq_results:
            raise error.TestFail("Can't find irq %s from procfs" % irq_watch)

    error.context("Load I/O in all targets", logging.info)
    get_dev_cmd = params.get("get_dev_cmd", "ls /dev/[svh]d*")
    output = session.cmd_output(get_dev_cmd)
    system_dev = re.findall("[svh]d(\w+)\d+", output)[0]
    dd_timeout = int(re.findall("\d+", extra_image_size)[0])
    fill_cmd = ""
    count = 0
    for dev in re.split("\s+", output):
        if not dev:
            continue
        if not re.findall("[svh]d%s" % system_dev, dev):
            fill_cmd += " dd of=%s if=/dev/urandom bs=1M " % dev
            fill_cmd += "count=%s &&" % dd_timeout
            count += 1
    if count != images_num:
        raise error.TestError("Disks are not all show up in system. Output "
                              "from the check command: %s" % output)
    fill_cmd = fill_cmd.rstrip("&&")
    session.cmd(fill_cmd, timeout=dd_timeout)

    error.context("Check the interrupt queues in guest", logging.info)
    output = session.cmd_output(irq_check_cmd)
    irq_results, cpu_list = proc_interrupts_results(output)
    irq_bit_map = 0
    for irq_watch in irqs_watch:
        if "request" in irq_results[irq_watch]["irq_des"]:
            for index, count in enumerate(irq_results[irq_watch]["count"]):
                if int(count) > 0:
                    irq_bit_map |= 2 ** index

    cpu_count = 0
    error_msg = ""
    cpu_not_used = []
    for index, cpu in enumerate(cpu_list):
        if 2 ** index & irq_bit_map != 2 ** index:
            cpu_not_used.append(cpu)

    if cpu_not_used:
        logging.debug("Interrupt info from procfs:\n%s" % output)
        error_msg = " ".join(cpu_not_used)
        if len(cpu_not_used) > 1:
            error_msg += " are"
        else:
            error_msg += " is"
        error_msg += " not used during test. Please check debug log for"
        error_msg += " more information."
        raise error.TestFail(error_msg)
