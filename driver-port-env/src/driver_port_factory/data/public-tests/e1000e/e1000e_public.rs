// SPDX-License-Identifier: MPL-2.0
// Operator-owned assertions; contains no device implementation.
use crate::e1000e;
const MAC: [u8; 6] = [0x52, 0x54, 0, 0x12, 0x34, 0x56];
pub fn run(command: &str) {
    let dev = crate::get_device("e1000e0").expect("real PCI/network registration");
    assert_eq!(dev.lock().mac_addr().0, MAC);
    assert!(dev.lock().capabilities().max_transmission_unit >= 1514);
    match command {
        "probe" => assert!(dev.lock().can_send()),
        "recovery" => {
            for _ in 0..3 {
                e1000e::stop().unwrap(); e1000e::stop().unwrap();
                assert!(!dev.lock().can_send());
                e1000e::start().unwrap(); e1000e::start().unwrap();
                assert!(dev.lock().can_send());
            }
        }
        "diagnostics" => {
            e1000e::stop().unwrap();
            let status = e1000e::read_register(0x8).unwrap();
            e1000e::write_register(0x8, 0x7fff_f033).unwrap();
            assert_eq!(e1000e::read_register(0x8).unwrap() & 0x7fff_f033, status & 0x7fff_f033);
            for (offset, mask) in [(0x28, 0xffff_ffff), (0x2c, 0xffff), (0x30, 0xffff), (0x38, 0xffff)] {
                let original = e1000e::read_register(offset).unwrap();
                for pattern in [0x5a5a_5a5a, 0xa5a5_a5a5, 0, 0xffff_ffff] {
                    e1000e::write_register(offset, pattern & mask).unwrap();
                    assert_eq!(e1000e::read_register(offset).unwrap() & mask, pattern & mask);
                }
                e1000e::write_register(offset, original).unwrap();
                assert_eq!(e1000e::read_register(offset).unwrap() & mask, original & mask);
            }
            let mut sum: u16 = 0;
            for offset in 0..64 { sum = sum.wrapping_add(e1000e::read_nvm(offset).unwrap()); }
            assert_eq!(sum, 0xbaba, "Linux NVM_SUM checksum");
            e1000e::start().unwrap();
            assert!(dev.lock().can_send());
        },
        _ => panic!("invalid public command"),
    }
    ostd::early_println!("DPF_INTEL_OK e1000e {}", command);
}
