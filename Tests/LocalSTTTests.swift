import XCTest
@testable import AudioWhisper

final class LocalSTTTests: XCTestCase {
    func testConfigurationUsesExistingDaemonAndReturnsChosenPort() async throws {
        let daemon = MLDaemonManager()
        await daemon.setTestResponder { method, params in
            XCTAssertEqual(method, "stt_configure")
            XCTAssertEqual(params["enabled"] as? Bool, true)
            XCTAssertEqual(params["port"] as? Int, 0)
            XCTAssertEqual(params["origins"] as? [String], ["http://localhost:3000"])
            return ["running": true, "port": 43210, "token_path": "/tmp/test.token", "error": NSNull()]
        }
        let status = try await daemon.configureSTT(enabled: true, port: 0, origins: ["http://localhost:3000"])
        XCTAssertTrue(status.running)
        XCTAssertEqual(status.port, 43210)
        XCTAssertNil(status.error)
    }

    func testPortConflictIsStatusRatherThanDaemonFailure() async throws {
        let daemon = MLDaemonManager()
        await daemon.setTestResponder { _, _ in
            ["running": false, "port": NSNull(), "token_path": "/tmp/test.token", "error": "Address already in use"]
        }
        let status = try await daemon.configureSTT(enabled: true, port: 8111, origins: [])
        XCTAssertFalse(status.running)
        XCTAssertNil(status.port)
        XCTAssertEqual(status.error, "Address already in use")
    }

    func testRecordingReservationWorksWithoutPreview() async {
        let daemon = MLDaemonManager()
        await daemon.setTestResponder { method, params in
            XCTAssertEqual(method, "recording_state")
            XCTAssertEqual(params["active"] as? Bool, true)
            return ["success": true]
        }
        await daemon.setRecordingActive(true)
    }
}
