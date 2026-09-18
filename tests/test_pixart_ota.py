import unittest

from tools import pixart_ota as ota


class CommandBuilderTests(unittest.TestCase):
    def test_builders_match_ota_utility_wire_vectors(self):
        self.assertEqual(ota.build_retransmit(), bytes.fromhex("28 00"))
        self.assertEqual(ota.build_legacy_init(), bytes.fromhex("10 00"))
        self.assertEqual(
            ota.build_init_new(0x12345678),
            bytes.fromhex("27 78 56 34 12 00"),
        )
        self.assertEqual(
            ota.build_object_create(0x12345678, 0x00000400),
            bytes.fromhex("25 78 56 34 12 00 04 00 00"),
        )
        self.assertEqual(
            ota.build_upgrade(0x12345678, 0xABCD, "1.0"),
            bytes.fromhex(
                "18 78 56 34 12 cd ab 31 2e 30 00 00 00 00 00 00 00"
            ),
        )
        self.assertEqual(ota.build_reset(), bytes.fromhex("22 00"))

    def test_builders_reject_out_of_range_integers(self):
        cases = (
            (ota.build_init_new, (-1,)),
            (ota.build_init_new, (0x1_0000_0000,)),
            (ota.build_object_create, (-1, 1)),
            (ota.build_object_create, (0, 0)),
            (ota.build_object_create, (0, 4097)),
            (ota.build_upgrade, (1, -1, "1.0")),
            (ota.build_upgrade, (1, 0x1_0000, "1.0")),
        )
        for builder, arguments in cases:
            with self.subTest(builder=builder.__name__, arguments=arguments):
                with self.assertRaises(ota.ProtocolError):
                    builder(*arguments)

    def test_upgrade_rejects_non_ascii_or_oversized_version(self):
        for version in ("日本語", "12345678901"):
            with self.subTest(version=version), self.assertRaises(ota.ProtocolError):
                ota.build_upgrade(1, 0, version)


class InitNewResponseTests(unittest.TestCase):
    VALID_FRAME = bytes.fromhex(
        "0e 10 27 00 01 02 00 34 12 00 10 00 00 f4 00 08 00 01"
    )

    def test_parse_init_new_response_golden_vector(self):
        self.assertEqual(
            ota.parse_init_new_response(self.VALID_FRAME),
            ota.OtaState(
                status=0,
                new_flow=1,
                offset=2,
                checksum=0x1234,
                max_object_size=4096,
                mtu_size=244,
                prn_threshold=8,
                spec_result=1,
            ),
        )

    def test_parse_rejects_malformed_envelope_or_length(self):
        frames = (
            self.VALID_FRAME[:-1],
            self.VALID_FRAME + b"\x00",
            b"\x0f" + self.VALID_FRAME[1:],
            self.VALID_FRAME[:1] + b"\x0f" + self.VALID_FRAME[2:],
            self.VALID_FRAME[:2] + b"\x26" + self.VALID_FRAME[3:],
        )
        for frame in frames:
            with self.subTest(frame=frame.hex()), self.assertRaises(ota.ProtocolError):
                ota.parse_init_new_response(frame)

    def test_parse_rejects_unsuccessful_fields(self):
        invalid_fields = {
            "status": {3: 1},
            "new-flow": {4: 0},
            "max-object-zero": {9: 0, 10: 0, 11: 0, 12: 0},
            "max-object-too-large": {9: 1, 10: 0x10, 11: 0, 12: 0},
            "mtu-zero": {13: 0, 14: 0},
            "prn-zero": {15: 0, 16: 0},
            "spec-result": {17: 0},
        }
        for name, mutations in invalid_fields.items():
            frame = bytearray(self.VALID_FRAME)
            for index, value in mutations.items():
                frame[index] = value
            with self.subTest(name=name), self.assertRaises(
                ota.ProtocolError
            ):
                ota.parse_init_new_response(bytes(frame))


class TransferPlanningTests(unittest.TestCase):
    def state(self, **changes):
        values = {
            "status": 0,
            "new_flow": 1,
            "offset": 0,
            "checksum": 0,
            "max_object_size": 4,
            "mtu_size": 3,
            "prn_threshold": 2,
            "spec_result": 1,
        }
        values.update(changes)
        return ota.OtaState(**values)

    def test_sum16_wraps_at_16_bits(self):
        self.assertEqual(ota.sum16(b"\xff" * 258), 0x00FE)

    def test_plan_declares_max_size_for_partial_final_object(self):
        operations = list(
            ota.iter_transfer_operations(
                b"\x01\x02\x03\x04\x05\x06", self.state(), "v1"
            )
        )

        self.assertEqual(
            operations,
            [
                ota.TransferOperation(
                    "object-create",
                    bytes.fromhex("25 00 00 00 00 04 00 00 00"),
                ),
                ota.TransferOperation("wait-object", expected_opcode=0x25),
                ota.TransferOperation("payload", b"\x01\x02\x03"),
                ota.TransferOperation("payload", b"\x04"),
                ota.TransferOperation(
                    "wait-prn", expected_opcode=0x17, expected_checksum=10
                ),
                ota.TransferOperation(
                    "object-create",
                    bytes.fromhex("25 04 00 00 00 04 00 00 00"),
                ),
                ota.TransferOperation("wait-object", expected_opcode=0x25),
                ota.TransferOperation("payload", b"\x05\x06"),
                ota.TransferOperation(
                    "wait-prn", expected_opcode=0x17, expected_checksum=21
                ),
                ota.TransferOperation(
                    "upgrade",
                    bytes.fromhex(
                        "18 06 00 00 00 15 00 76 31 00 00 00 00 00 00 00 00"
                    ),
                ),
                ota.TransferOperation("wait-upgrade", expected_opcode=0x18),
                ota.TransferOperation("reset", bytes.fromhex("22 00")),
            ],
        )

    def test_plan_waits_at_prn_threshold_and_object_end_with_checksum(self):
        operations = list(
            ota.iter_transfer_operations(
                bytes(range(1, 9)),
                self.state(max_object_size=8, mtu_size=2, prn_threshold=3),
                "v1",
            )
        )
        waits = [operation for operation in operations if operation.kind == "wait-prn"]
        self.assertEqual(
            waits,
            [
                ota.TransferOperation(
                    "wait-prn", expected_opcode=0x17, expected_checksum=21
                ),
                ota.TransferOperation(
                    "wait-prn", expected_opcode=0x17, expected_checksum=36
                ),
            ],
        )

    def test_plan_uses_explicit_payload_chunk_size_for_prn_windows(self):
        operations = list(
            ota.iter_transfer_operations(
                bytes(range(1, 9)),
                self.state(max_object_size=8, mtu_size=4, prn_threshold=3),
                "v1",
                payload_chunk_size=2,
            )
        )

        self.assertEqual(
            [
                operation.payload
                for operation in operations
                if operation.kind == "payload"
            ],
            [b"\x01\x02", b"\x03\x04", b"\x05\x06", b"\x07\x08"],
        )
        self.assertEqual(
            [
                operation.expected_checksum
                for operation in operations
                if operation.kind == "wait-prn"
            ],
            [21, 36],
        )

    def test_plan_rejects_invalid_explicit_payload_chunk_size(self):
        for chunk_size in (False, 0, -1, 1.5, 4):
            with self.subTest(chunk_size=chunk_size), self.assertRaises(
                ota.ProtocolError
            ):
                list(
                    ota.iter_transfer_operations(
                        b"\x01",
                        self.state(mtu_size=3),
                        "v1",
                        payload_chunk_size=chunk_size,
                    )
                )

    def test_matching_resume_starts_at_reported_object_and_running_checksum(self):
        operations = list(
            ota.iter_transfer_operations(
                b"\x01\x02\x03\x04\x05\x06",
                self.state(offset=1, checksum=10),
                "v1",
            )
        )

        self.assertEqual(operations[0].kind, "object-create")
        self.assertEqual(
            operations[0].payload,
            bytes.fromhex("25 04 00 00 00 04 00 00 00"),
        )
        wait = next(operation for operation in operations if operation.kind == "wait-prn")
        self.assertEqual(wait.expected_checksum, 21)

    def test_invalid_resume_restarts_from_zero(self):
        cases = (
            self.state(offset=1, checksum=11),
            self.state(offset=3, checksum=21),
        )
        for state in cases:
            with self.subTest(state=state):
                operations = list(
                    ota.iter_transfer_operations(
                        b"\x01\x02\x03\x04\x05\x06", state, "v1"
                    )
                )
                self.assertEqual(
                    operations[0].payload,
                    bytes.fromhex("25 00 00 00 00 04 00 00 00"),
                )
                wait = next(
                    operation
                    for operation in operations
                    if operation.kind == "wait-prn"
                    and operation.expected_checksum is not None
                )
                self.assertEqual(wait.expected_checksum, 10)

    def test_completed_resume_only_finalizes(self):
        operations = list(
            ota.iter_transfer_operations(
                b"\x01\x02\x03\x04\x05\x06",
                self.state(offset=2, checksum=21),
                "v1",
            )
        )
        self.assertEqual(
            [operation.kind for operation in operations],
            ["upgrade", "wait-upgrade", "reset"],
        )

    def test_plan_rejects_empty_firmware_or_invalid_state(self):
        invalid_inputs = (
            (b"", self.state()),
            (b"\x01", self.state(max_object_size=0)),
            (b"\x01", self.state(max_object_size=4097)),
            (b"\x01", self.state(mtu_size=0)),
            (b"\x01", self.state(prn_threshold=0)),
        )
        for data, state in invalid_inputs:
            with self.subTest(data=data, state=state), self.assertRaises(
                ota.ProtocolError
            ):
                list(ota.iter_transfer_operations(data, state, "v1"))

    def test_plan_rejects_non_integer_or_out_of_range_state_fields(self):
        invalid_values = {
            "status": (False, 0.0, -1, 0x100),
            "new_flow": (True, 1.0, -1, 0x100),
            "offset": (False, 0.0, -1, 0x1_0000),
            "checksum": (False, 0.0, -1, 0x1_0000),
            "max_object_size": (True, 4.0, -1, 0x1_0000_0000),
            "mtu_size": (True, 3.0, -1, 0x1_0000),
            "prn_threshold": (True, 2.0, -1, 0x1_0000),
            "spec_result": (True, 1.0, -1, 0x100),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    caught = None
                    try:
                        list(
                            ota.iter_transfer_operations(
                                b"\x01", self.state(**{field: value}), "v1"
                            )
                        )
                    except Exception as exc:
                        caught = exc
                    self.assertIsInstance(caught, ota.ProtocolError)

    def test_plan_rejects_invalid_version_before_first_operation(self):
        operations = ota.iter_transfer_operations(b"\x01", self.state(), "日本語")
        with self.assertRaises(ota.ProtocolError):
            next(operations)


if __name__ == "__main__":
    unittest.main()
