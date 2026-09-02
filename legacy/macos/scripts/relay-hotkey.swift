import AppKit
import Carbon
import Foundation

let scriptPath = "/Users/papa/codebase/n8n/scripts/relay-from-clipboard.sh"
let signature: OSType = 0x4D524C59 // MRLY
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let handler: EventHandlerUPP = { _, _, _ in
    fputs("Market relay hotkey pressed\n", stdout)
    fflush(stdout)
    DispatchQueue.global().async {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: scriptPath)
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
        } catch {
            fputs("Failed to launch relay script: \(error)\n", stderr)
        }
    }
    return noErr
}

var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
let installStatus = InstallEventHandler(GetApplicationEventTarget(), handler, 1, &eventType, nil, nil)
guard installStatus == noErr else {
    fputs("Unable to install hotkey event handler: \(installStatus)\n", stderr)
    exit(1)
}

var hotKey: EventHotKeyRef?
var hotKeyID = EventHotKeyID(signature: signature, id: 1)
let modifiers = UInt32(controlKey | optionKey | cmdKey)
let registerStatus = RegisterEventHotKey(UInt32(kVK_ANSI_R), modifiers, hotKeyID, GetApplicationEventTarget(), 0, &hotKey)
guard registerStatus == noErr else {
    fputs("Unable to register Control-Option-Command-R: \(registerStatus)\n", stderr)
    exit(1)
}

print("Market relay hotkey is active: Control-Option-Command-R")
fflush(stdout)
app.run()
