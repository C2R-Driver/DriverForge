// SPDX-License-Identifier: MPL-2.0
// Public real-device scenario; no translated device implementation.
use alloc::{sync::Arc, vec};
use core::sync::atomic::{AtomicUsize, Ordering};

use ostd::mm::VmWriter;

use crate::NetError;

static CALLBACKS: AtomicUsize = AtomicUsize::new(0);
const MAC: [u8; 6] = [0x52, 0x54, 0, 0x12, 0x34, 0x56];
const PEER: [u8; 6] = [0x52, 0x54, 0, 0xab, 0xcd, 0xef];

fn exchange(sequence: u16, length: usize) {
    let dev = crate::get_device("ne2k0").expect("registered ne2k0");
    let mut bytes = vec![0u8; length];
    bytes[..6].copy_from_slice(&PEER);
    bytes[6..12].copy_from_slice(&MAC);
    bytes[12..14].copy_from_slice(&[0x88, 0xb5]);
    bytes[14..16].copy_from_slice(&sequence.to_be_bytes());
    for (i, b) in bytes[16..].iter_mut().enumerate() {
        *b = (sequence as u8).wrapping_add((i as u8).wrapping_mul(17));
    }
    let mut ready = false;
    for _ in 0..20_000_000 {
        {
            let mut device = dev.lock();
            device.free_processed_tx_buffers();
            ready = device.can_send();
        }
        if ready {
            break;
        }
        core::hint::spin_loop();
    }
    assert!(ready, "TX completion bounded wait seq {}", sequence);
    let callbacks = CALLBACKS.load(Ordering::Acquire);
    dev.lock().send(&bytes).expect("send");
    // No receive polling before the hardware-driven network callback.
    for _ in 0..40_000_000 {
        if CALLBACKS.load(Ordering::Acquire) > callbacks {
            break;
        }
        core::hint::spin_loop();
    }
    assert!(
        CALLBACKS.load(Ordering::Acquire) > callbacks,
        "no real RX callback seq {}",
        sequence
    );
    let packet = {
        let mut device = dev.lock();
        let packet = device.receive().expect("RX after callback");
        device.notify_poll_end();
        packet
    };
    let mut actual = vec![0u8; length + 8];
    let size = packet
        .payload()
        .read(&mut VmWriter::from(actual.as_mut_slice()));
    assert_eq!(size, length, "frame length seq {}", sequence);
    bytes[..6].copy_from_slice(&MAC);
    bytes[6..12].copy_from_slice(&PEER);
    bytes[13] = 0xb6;
    for b in &mut bytes[16..] {
        *b ^= 0xa5;
    }
    assert_eq!(
        &actual[..length],
        bytes.as_slice(),
        "wire response seq {}",
        sequence
    );
    ostd::early_println!("DPF_NE2K_EXCHANGE {} {}", sequence, length);
}

pub fn run(scope: &str) {
    assert!(matches!(scope, "probe" | "traffic" | "recovery"));
    crate::ne2k::register().expect("PCI probe and network registration");
    assert_eq!(
        crate::all_devices()
            .iter()
            .filter(|(name, _)| name == "ne2k0")
            .count(),
        1
    );
    let dev = crate::get_device("ne2k0").unwrap();
    assert_eq!(dev.lock().mac_addr().0, MAC);
    assert!(dev.lock().capabilities().max_transmission_unit >= 1514);
    crate::register_recv_callback("ne2k0", || {
        CALLBACKS.fetch_add(1, Ordering::Release);
    });
    ostd::early_println!("DPF_NE2K_PROBE_OK");
    if scope == "traffic" {
        let hz = ostd::arch::tsc_freq();
        assert!(hz > 0);
        let mut trigger = [0u8; 60];
        trigger[..6].copy_from_slice(&PEER);
        trigger[6..12].copy_from_slice(&MAC);
        // Unanswered Ethernet frames: only TX completion, no RX completion.
        trigger[12..14].copy_from_slice(&[0x88, 0xb7]);
        let guard = ostd::irq::disable_local();
        dev.lock().send(&trigger).expect("initial TX");
        if dev.lock().can_send() { dev.lock().send(&trigger).expect("fill pending TX"); }
        assert!(!dev.lock().can_send(), "test must have outstanding TX");
        ostd::early_println!("NE2K_TX_FAULT_READY");
        // Host pauses vCPUs and clears TX-complete bits before interrupts resume.
        let start = ostd::arch::read_tsc();
        while ostd::arch::read_tsc().wrapping_sub(start) < hz * 2 { core::hint::spin_loop(); }
        drop(guard);
        let start = ostd::arch::read_tsc();
        let mut recovered = false;
        while ostd::arch::read_tsc().wrapping_sub(start) < hz * 5 {
            let mut device = dev.lock();
            device.free_processed_tx_buffers();
            recovered = device.can_send();
            drop(device);
            if recovered { break; }
            core::hint::spin_loop();
        }
        if recovered {
            ostd::early_println!("NE2K_TX_RECOVERED");
            exchange(5000, 1514);
            ostd::early_println!("NE2K_TX_FAULT_PASS");
        } else {
            ostd::early_println!("NE2K_TX_FAULT_FAIL stuck-busy");
        }
    }
    if scope == "recovery" {
        for round in 0..3 {
            crate::ne2k::stop().unwrap();
            crate::ne2k::stop().unwrap();
            assert!(!dev.lock().can_send());
            assert!(!dev.lock().can_receive());
            assert!(matches!(
                dev.lock().send(&[0u8; 60]),
                Err(NetError::NotReady) | Err(NetError::Busy)
            ));
            let count = CALLBACKS.load(Ordering::Acquire);
            for _ in 0..100_000 {
                core::hint::spin_loop();
            }
            assert_eq!(CALLBACKS.load(Ordering::Acquire), count);
            crate::ne2k::start().unwrap();
            crate::ne2k::start().unwrap();
            exchange(1000 + round, 1514);
        }
        ostd::early_println!("DPF_NE2K_RECOVERY_OK");
    }
    crate::ne2k::stop().unwrap();
    assert!(!dev.lock().can_send());
    // Target registry has no unregister API; retain binding but quiesce hardware.
    assert!(Arc::strong_count(&dev) >= 2);
    ostd::early_println!("DPF_NE2K_PUBLIC_OK {}", scope);
}
