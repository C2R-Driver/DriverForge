// SPDX-License-Identifier: MPL-2.0
// Operator-owned assertions; contains no device implementation.
use crate::igb;
const MAC: [u8; 6] = [0x52, 0x54, 0, 0x12, 0x34, 0x56];
pub fn run(command: &str) {
    let dev = crate::get_device("igb0").expect("real PCI/network registration");
    assert_eq!(dev.lock().mac_addr().0, MAC);
    assert!(dev.lock().capabilities().max_transmission_unit >= 1514);
    match command {
        "probe" => assert!(dev.lock().can_send()),
        "recovery" => {
            for _ in 0..3 {
                igb::stop().unwrap(); igb::stop().unwrap();
                assert!(!dev.lock().can_send());
                igb::start().unwrap(); igb::start().unwrap();
                assert!(dev.lock().can_send());
            }
        }
        "queues2" => { igb::set_queues(2).unwrap(); },
        _ => panic!("invalid public command"),
    }
    ostd::early_println!("DPF_INTEL_OK igb {}", command);
}
