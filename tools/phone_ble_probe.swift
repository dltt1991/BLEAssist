#!/usr/bin/env swift
import Foundation
import CoreBluetooth

let advertisedName = "YS"
let serviceUUIDString = "0000FFF0-0000-1000-8000-00805F9B34FB"
let notifyUUIDString = "0000FFF1-0000-1000-8000-00805F9B34FB"
let writeUUIDString = "0000FFF2-0000-1000-8000-00805F9B34FB"
let configUUIDString = "0000FFF3-0000-1000-8000-00805F9B34FB"
let auxServiceUUIDString = "0000FFE0-0000-1000-8000-00805F9B34FB"
let auxNotifyUUIDString = "0000FFE1-0000-1000-8000-00805F9B34FB"
let auxWriteUUIDString = "0000FFE2-0000-1000-8000-00805F9B34FB"
let uartDataDescription = "UART DATA"
let bleDataDescription = "BLE DATA"
let bleConfigDescription = "BLE CONFIG"
let publishedServiceCount = 2
let responseDelayMilliseconds = 200

final class FrameDecoder {
    private var buffer = Data()

    func feed(_ data: Data) -> [String] {
        buffer.append(data)
        var frames: [String] = []

        while let start = buffer.firstIndex(of: Character("Y").asciiValue!) {
            if start > buffer.startIndex {
                buffer.removeSubrange(buffer.startIndex..<start)
            }
            guard let end = buffer.dropFirst().firstIndex(of: Character("S").asciiValue!) else {
                if buffer.count > 256 {
                    buffer = Data(buffer.suffix(256))
                }
                break
            }
            let frameData = buffer[buffer.startIndex...end]
            buffer.removeSubrange(buffer.startIndex...end)
            if let frame = String(data: frameData, encoding: .ascii) {
                frames.append(frame)
            }
        }

        if !buffer.contains(Character("Y").asciiValue!) {
            buffer.removeAll(keepingCapacity: true)
        }
        return frames
    }
}

func responseFrame(for frame: String) -> String? {
    switch frame {
    case "Y2AS": return "Y1AS"
    case "Y2BS": return "Y1BS"
    case "Y2WS": return nil
    default:
        if frame.count == 5,
           frame.hasPrefix("Y2O"), frame.hasSuffix("S"),
           let mode = frame.dropFirst(3).first, ("1"..."6").contains(String(mode)) {
            return "Y1O\(mode)S"
        }
        if frame.hasPrefix("Y2C"), frame.hasSuffix("S") {
            return "Y1CS"
        }
        return nil
    }
}

func hexString(_ data: Data) -> String {
    data.map { String(format: "%02x", $0) }.joined(separator: " ")
}

func printableASCII(_ data: Data) -> String {
    String(data.map {
        (0x20...0x7e).contains($0) ? Character(UnicodeScalar($0)) : "."
    })
}

func isProtocolNotifyUUID(_ uuid: CBUUID) -> Bool {
    uuid == CBUUID(string: notifyUUIDString)
}

func wireResponseData(_ frame: String) -> Data {
    Data(frame.utf8) + Data([0xaa, 0xaa, 0xaa])
}

private let logDateFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.dateFormat = "HH:mm:ss.SSS"
    return formatter
}()

func timestamp() -> String {
    logDateFormatter.string(from: Date())
}

func runSelfTests() {
    precondition(advertisedName == "YS")
    precondition(CBUUID(string: serviceUUIDString).data.count == 16)
    precondition(CBUUID(string: notifyUUIDString).data.count == 16)
    precondition(CBUUID(string: writeUUIDString).data.count == 16)
    precondition(auxServiceUUIDString == "0000FFE0-0000-1000-8000-00805F9B34FB")
    precondition(uartDataDescription == "UART DATA")
    precondition(bleDataDescription == "BLE DATA")
    precondition(bleConfigDescription == "BLE CONFIG")
    precondition(publishedServiceCount == 2)
    precondition(responseDelayMilliseconds == 200)
    precondition(isProtocolNotifyUUID(CBUUID(string: notifyUUIDString)))
    precondition(!isProtocolNotifyUUID(CBUUID(string: configUUIDString)))
    precondition(!isProtocolNotifyUUID(CBUUID(string: auxNotifyUUIDString)))
    precondition(wireResponseData("Y1AS") == Data([0x59, 0x31, 0x41, 0x53, 0xaa, 0xaa, 0xaa]))
    let decoder = FrameDecoder()
    precondition(decoder.feed(Data("noiseY2".utf8)).isEmpty)
    precondition(decoder.feed(Data("ASY2BS".utf8)) == ["Y2AS", "Y2BS"])
    precondition(decoder.feed(Data("Y2C5005003SY2WS".utf8)) == ["Y2C5005003S", "Y2WS"])

    precondition(responseFrame(for: "Y2AS") == "Y1AS")
    precondition(responseFrame(for: "Y2BS") == "Y1BS")
    precondition(responseFrame(for: "Y2O6S") == "Y1O6S")
    precondition(responseFrame(for: "Y2C5005003S") == "Y1CS")
    precondition(responseFrame(for: "Y2WS") == nil)
    precondition(responseFrame(for: "invalid") == nil)
    precondition(hexString(Data([0x59, 0x32, 0x41, 0x53])) == "59 32 41 53")
    precondition(printableASCII(Data([0x59, 0x00, 0x53])) == "Y.S")
    print("SELF-TEST PASS")
}

final class Probe: NSObject, CBPeripheralManagerDelegate {
    private let serviceUUID = CBUUID(string: serviceUUIDString)
    private let notifyUUID = CBUUID(string: notifyUUIDString)
    private let writeUUID = CBUUID(string: writeUUIDString)
    private let configUUID = CBUUID(string: configUUIDString)
    private let auxServiceUUID = CBUUID(string: auxServiceUUIDString)
    private let auxNotifyUUID = CBUUID(string: auxNotifyUUIDString)
    private let auxWriteUUID = CBUUID(string: auxWriteUUIDString)
    private var manager: CBPeripheralManager!
    private var notifyCharacteristic: CBMutableCharacteristic!
    private var decoder = FrameDecoder()
    private var lastWriteTime: Date?
    private var pendingNotifications: [Data] = []
    private var subscriberCount = 0
    private var pendingServices = 0

    override init() {
        super.init()
        manager = CBPeripheralManager(delegate: self, queue: nil)
    }

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        guard peripheral.state == .poweredOn else {
            print("[\(timestamp())] Bluetooth state: \(peripheral.state.rawValue); waiting for poweredOn")
            return
        }

        let userDescriptionUUID = CBUUID(string: CBUUIDCharacteristicUserDescriptionString)

        notifyCharacteristic = CBMutableCharacteristic(
            type: notifyUUID,
            properties: [.read, .notify],
            value: nil,
            permissions: [.readable]
        )
        notifyCharacteristic.descriptors = [
            CBMutableDescriptor(type: userDescriptionUUID, value: uartDataDescription)
        ]
        let writeCharacteristic = CBMutableCharacteristic(
            type: writeUUID,
            properties: [.write, .writeWithoutResponse],
            value: nil,
            permissions: [.writeable]
        )
        writeCharacteristic.descriptors = [
            CBMutableDescriptor(type: userDescriptionUUID, value: bleDataDescription)
        ]
        let configCharacteristic = CBMutableCharacteristic(
            type: configUUID,
            properties: [.read, .write, .notify],
            value: nil,
            permissions: [.readable, .writeable]
        )
        configCharacteristic.descriptors = [
            CBMutableDescriptor(type: userDescriptionUUID, value: bleConfigDescription)
        ]
        let service = CBMutableService(type: serviceUUID, primary: true)
        service.characteristics = [
            notifyCharacteristic,
            writeCharacteristic,
            configCharacteristic,
        ]

        let auxNotifyCharacteristic = CBMutableCharacteristic(
            type: auxNotifyUUID,
            properties: [.notify],
            value: nil,
            permissions: []
        )
        auxNotifyCharacteristic.descriptors = [
            CBMutableDescriptor(type: userDescriptionUUID, value: uartDataDescription)
        ]
        let auxWriteCharacteristic = CBMutableCharacteristic(
            type: auxWriteUUID,
            properties: [.write, .writeWithoutResponse],
            value: nil,
            permissions: [.writeable]
        )
        auxWriteCharacteristic.descriptors = [
            CBMutableDescriptor(type: userDescriptionUUID, value: bleDataDescription)
        ]
        let auxService = CBMutableService(type: auxServiceUUID, primary: true)
        auxService.characteristics = [auxNotifyCharacteristic, auxWriteCharacteristic]

        peripheral.removeAllServices()
        pendingServices = publishedServiceCount
        peripheral.add(service)
        peripheral.add(auxService)
    }

    func peripheralManager(
        _ peripheral: CBPeripheralManager,
        didAdd service: CBService,
        error: Error?
    ) {
        if let error {
            print("[\(timestamp())] ERROR adding \(service.uuid): \(error.localizedDescription)")
            exit(EXIT_FAILURE)
        }
        pendingServices -= 1
        guard pendingServices == 0 else { return }
        peripheral.startAdvertising([
            CBAdvertisementDataLocalNameKey: advertisedName,
            CBAdvertisementDataServiceUUIDsKey: [serviceUUID],
        ])
    }

    func peripheralManagerDidStartAdvertising(
        _ peripheral: CBPeripheralManager,
        error: Error?
    ) {
        if let error {
            print("[\(timestamp())] ERROR advertising: \(error.localizedDescription)")
            exit(EXIT_FAILURE)
        }
        print("[\(timestamp())] Advertising \(advertisedName) (FFF0); select it in the phone App")
    }

    func peripheralManager(
        _ peripheral: CBPeripheralManager,
        central: CBCentral,
        didSubscribeTo characteristic: CBCharacteristic
    ) {
        print("[\(timestamp())] SUBSCRIBED \(characteristic.uuid.uuidString); max notification \(central.maximumUpdateValueLength) bytes")
        guard isProtocolNotifyUUID(characteristic.uuid) else { return }
        subscriberCount += 1
        flushNotifications()
    }

    func peripheralManager(
        _ peripheral: CBPeripheralManager,
        central: CBCentral,
        didUnsubscribeFrom characteristic: CBCharacteristic
    ) {
        print("[\(timestamp())] UNSUBSCRIBED \(characteristic.uuid.uuidString)")
        guard isProtocolNotifyUUID(characteristic.uuid) else { return }
        subscriberCount = max(0, subscriberCount - 1)
    }

    func peripheralManager(
        _ peripheral: CBPeripheralManager,
        didReceiveWrite requests: [CBATTRequest]
    ) {
        guard !requests.isEmpty else { return }

        for request in requests {
            guard request.characteristic.uuid == writeUUID else {
                peripheral.respond(to: request, withResult: .requestNotSupported)
                return
            }
            guard request.offset == 0 else {
                peripheral.respond(to: request, withResult: .invalidOffset)
                return
            }
            guard request.value != nil else {
                peripheral.respond(to: request, withResult: .invalidAttributeValueLength)
                return
            }
        }

        peripheral.respond(to: requests[0], withResult: .success)
        for request in requests {
            capture(request.value!)
        }
    }

    func peripheralManager(
        _ peripheral: CBPeripheralManager,
        didReceiveRead request: CBATTRequest
    ) {
        guard request.offset == 0 else {
            peripheral.respond(to: request, withResult: .invalidOffset)
            return
        }
        guard request.characteristic.uuid == notifyUUID ||
              request.characteristic.uuid == configUUID else {
            peripheral.respond(to: request, withResult: .requestNotSupported)
            return
        }
        request.value = Data()
        peripheral.respond(to: request, withResult: .success)
    }

    private func capture(_ data: Data) {
        let now = Date()
        let interval = lastWriteTime.map {
            String(format: "%7.1f ms", now.timeIntervalSince($0) * 1000)
        } ?? "    first"
        lastWriteTime = now
        print("[\(timestamp())] RX Δ=\(interval) hex=[\(hexString(data))] ascii=\(printableASCII(data))")

        for frame in decoder.feed(data) {
            print("[\(timestamp())] FRAME \(frame)")
            if let response = responseFrame(for: frame) {
                DispatchQueue.main.asyncAfter(
                    deadline: .now() + .milliseconds(responseDelayMilliseconds)
                ) { [weak self] in
                    self?.notify(response)
                }
            }
        }
    }

    private func notify(_ frame: String) {
        let data = wireResponseData(frame)
        guard subscriberCount > 0 else {
            pendingNotifications.append(data)
            print("[\(timestamp())] QUEUED \(frame) (FFF1 not subscribed)")
            return
        }
        if manager.updateValue(data, for: notifyCharacteristic, onSubscribedCentrals: nil) {
            print("[\(timestamp())] TX \(frame) [aa aa aa]")
        } else {
            pendingNotifications.append(data)
            print("[\(timestamp())] QUEUED \(frame) (notification queue full)")
        }
    }

    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) {
        flushNotifications()
    }

    private func flushNotifications() {
        while subscriberCount > 0, let data = pendingNotifications.first {
            guard manager.updateValue(
                data,
                for: notifyCharacteristic,
                onSubscribedCentrals: nil
            ) else { return }
            pendingNotifications.removeFirst()
            print("[\(timestamp())] TX \(String(data: data, encoding: .ascii) ?? hexString(data))")
        }
    }
}

if CommandLine.arguments.contains("--self-test") {
    runSelfTests()
    exit(EXIT_SUCCESS)
}

print("Phone BLE Probe — press Control-C to stop")
let probe = Probe()
withExtendedLifetime(probe) {
    RunLoop.main.run()
}
