// SPDX-License-Identifier: MPL-2.0
// Public real-device scenario; no translated device implementation.
use alloc::{sync::Arc, vec};
use core::sync::atomic::{AtomicUsize, Ordering};

use ostd::mm::VmWriter;

use crate::NetError;

static CALLBACKS: AtomicUsize = AtomicUsize::new(0);
const MAC: [u8; 6] = [0x52, 0x54, 0, 0x12, 0x34, 0x56];
const PEER: [u8; 6] = [0x52, 0x54, 0, 0xab, 0xcd, 0xef];

fn burst_exchange() {
    let dev = crate::get_device("ne2k0").unwrap();
    let mut bytes = [0u8; 1514];
    bytes[..6].copy_from_slice(&PEER);
    bytes[6..12].copy_from_slice(&MAC);
    bytes[12..14].copy_from_slice(&[0x88, 0xb5]);
    bytes[14..16].copy_from_slice(&2000u16.to_be_bytes());
    for (i, b) in bytes[16..].iter_mut().enumerate() {
        *b = (2000u16 as u8).wrapping_add((i as u8).wrapping_mul(17));
    }
    let callbacks = CALLBACKS.load(Ordering::Acquire);
    let guard = ostd::irq::disable_local();
    dev.lock().send(&bytes).expect("burst trigger send");
    // Operator scoring: 128 maximum frames exceed the physical RX ring.
    // QEMU backpressures a full ring; this does not inject ENISR_OVER.
    let hz = ostd::arch::tsc_freq();
    assert!(hz > 0, "configured environment must expose calibrated TSC");
    let start = ostd::arch::read_tsc();
    while ostd::arch::read_tsc().wrapping_sub(start) < hz / 4 {
        core::hint::spin_loop();
    }
    drop(guard);
    let mut received = 0usize;
    let mut seen = [false; 128];
    let start = ostd::arch::read_tsc();
    let mut quiet = start;
    while ostd::arch::read_tsc().wrapping_sub(start) < hz * 3 {
        let packet = {
            let mut device = dev.lock();
            if !device.can_receive() {
                drop(device);
                if ostd::arch::read_tsc().wrapping_sub(quiet) > hz / 2 { break; }
                core::hint::spin_loop();
                continue;
            }
            let packet = device.receive().expect("pressure receive");
            device.notify_poll_end();
            packet
        };
        quiet = ostd::arch::read_tsc();
        let mut actual = [0u8; 1522];
        let size = packet.payload().read(&mut VmWriter::from(&mut actual[..]));
        assert_eq!(size, 1514);
        let sequence = u16::from_be_bytes([actual[14], actual[15]]);
        assert!((2000..2128).contains(&sequence), "unexpected pressure sequence");
        assert!(!seen[(sequence - 2000) as usize], "duplicate pressure frame");
        seen[(sequence - 2000) as usize] = true;
        bytes[..6].copy_from_slice(&MAC);
        bytes[6..12].copy_from_slice(&PEER);
        bytes[13] = 0xb6;
        bytes[14..16].copy_from_slice(&sequence.to_be_bytes());
        for (i, b) in bytes[16..].iter_mut().enumerate() {
            *b = (sequence as u8).wrapping_add((i as u8).wrapping_mul(17)) ^ 0xa5;
        }
        assert_eq!(&actual[..1514], &bytes[..], "pressure payload {}", sequence);
        received += 1;
        ostd::early_println!("DPF_NE2K_BURST_RX {}", sequence);
    }
    assert!(received > 0, "no pressure frames received");
    assert!(
        CALLBACKS.load(Ordering::Acquire) > callbacks,
        "burst requires hardware RX callback"
    );
    dev.lock().notify_poll_end();
    ostd::early_println!("DPF_NE2K_BURST_OK");
}

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
        // Over 32 KiB of traffic advances the physical 8390 receive ring repeatedly.
        for sequence in 0..4 {
            exchange(
                sequence,
                [60, 61, 127, 256, 513, 1514][sequence as usize % 6],
            );
        }
        burst_exchange();
        for sequence in 5000..5008 { exchange(sequence, 1514); }
        ostd::early_println!("NE2K_SEMANTIC_RECOVERY_PASS");
        ostd::early_println!("DPF_NE2K_TRAFFIC_OK");
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
