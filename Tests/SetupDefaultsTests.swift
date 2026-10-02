import XCTest
import AVFoundation
@testable import AudioWhisper

@MainActor
final class SetupDefaultsTests: XCTestCase {
    func testFreshDefaultsMatchEstablishedDictationConfiguration() {
        let name = "ScribeKitt.DefaultsTests.\(UUID())"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        AppDefaults.register(using: defaults)
        XCTAssertEqual(PressAndHoldSettings.configuration(using: defaults),
                       PressAndHoldConfiguration(enabled: true, key: .rightCommand, mode: .hold))
        XCTAssertTrue(defaults.autoBoostMicrophoneVolume)
        XCTAssertTrue(defaults.bool(forKey: AppDefaults.Keys.transcriptionStreaming))
        XCTAssertTrue(defaults.bool(forKey: AppDefaults.Keys.addTrailingSpace))
        XCTAssertTrue(defaults.bool(forKey: AppDefaults.Keys.localhostSTTEnabled))
        XCTAssertEqual(defaults.integer(forKey: AppDefaults.Keys.localhostSTTPort), 8111)
        XCTAssertTrue(defaults.bool(forKey: AppDefaults.Keys.transcriptionHistoryEnabled))
        XCTAssertTrue(defaults.bool(forKey: AppDefaults.Keys.playCompletionSound))
        XCTAssertFalse(defaults.bool(forKey: AppDefaults.Keys.immediateRecording))
        XCTAssertEqual(defaults.string(forKey: AppDefaults.Keys.transcriptionRetentionPeriod), "forever")
        XCTAssertEqual(defaults.string(forKey: AppDefaults.Keys.globalHotkey), "⌥⌃Q")
        XCTAssertEqual(defaults.string(forKey: AppDefaults.Keys.selectedParakeetModel), ParakeetModel.v2English.rawValue)
        defaults.set(false, forKey: AppDefaults.Keys.pressAndHoldEnabled)
        defaults.set(false, forKey: AppDefaults.Keys.localhostSTTEnabled)
        defaults.set(0, forKey: AppDefaults.Keys.localhostSTTPort)
        defaults.set(false, forKey: AppDefaults.Keys.autoBoostMicrophoneVolume)
        defaults.set(false, forKey: AppDefaults.Keys.transcriptionHistoryEnabled)
        defaults.set("oneWeek", forKey: AppDefaults.Keys.transcriptionRetentionPeriod)
        AppDefaults.register(using: defaults)
        XCTAssertFalse(defaults.bool(forKey: AppDefaults.Keys.localhostSTTEnabled))
        XCTAssertEqual(defaults.integer(forKey: AppDefaults.Keys.localhostSTTPort), 0)
        XCTAssertFalse(PressAndHoldSettings.configuration(using: defaults).enabled)
        XCTAssertFalse(defaults.autoBoostMicrophoneVolume)
        XCTAssertFalse(defaults.bool(forKey: AppDefaults.Keys.transcriptionHistoryEnabled))
        XCTAssertEqual(defaults.string(forKey: AppDefaults.Keys.transcriptionRetentionPeriod), "oneWeek")
    }

    func testPermissionSetupRequiresMicrophoneAndModifierAccessWithoutPromptingOnRead() async {
        var microphone: AVAuthorizationStatus = .notDetermined
        var keyboard = false, prompted = false
        let permissions = SetupPermissions(readMicrophone: { microphone }, readKeyboard: { keyboard },
            requestMicrophone: { microphone = .authorized; return true },
            openKeyboard: { prompted = true }, openMicrophone: { XCTFail("Fresh mic should request access") })
        XCTAssertTrue(permissions.needsSetup(configuration: .defaults))
        XCTAssertFalse(prompted)
        await permissions.allowMicrophone()
        XCTAssertTrue(permissions.microphoneAllowed)
        XCTAssertTrue(permissions.needsSetup(configuration: .defaults))
        permissions.allowKeyboard()
        XCTAssertTrue(prompted)
        keyboard = true
        permissions.refresh()
        XCTAssertFalse(permissions.needsSetup(configuration: .defaults))
    }

    func testDeniedMicrophoneOpensSettingsAndMenuOnlyNeedsNoKeyboardAccess() async {
        var opened = false
        let permissions = SetupPermissions(readMicrophone: { .denied }, readKeyboard: { false },
            requestMicrophone: { XCTFail("Denied permission cannot be reprompted"); return false },
            openKeyboard: {}, openMicrophone: { opened = true })
        await permissions.allowMicrophone()
        XCTAssertTrue(opened)
        XCTAssertFalse(permissions.microphoneAllowed)
        let menuOnly = SetupPermissions(readMicrophone: { .authorized }, readKeyboard: { false },
            requestMicrophone: { true }, openKeyboard: {}, openMicrophone: {})
        XCTAssertFalse(menuOnly.needsSetup(configuration: .init(enabled: false, key: .rightCommand, mode: .hold)))
    }
}
