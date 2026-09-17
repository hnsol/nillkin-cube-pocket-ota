import unittest

from tools import ota_protocol as protocol


class ReadOnlyCommandTests(unittest.TestCase):
    def test_fw_info_command_has_vendor_write_mode_and_exact_payload(self):
        spec = protocol.READ_ONLY_COMMANDS[0x23]

        self.assertEqual(spec.request, bytes.fromhex("23 00"))
        self.assertIs(spec.write_mode, protocol.WriteMode.WITH_RESPONSE)

    def test_vendor_model_queries_use_confirmed_payloads_and_write_mode(self):
        self.assertEqual(protocol.ModelIdentity.UNAVAILABLE.value, "unavailable")
        self.assertEqual(set(protocol.READ_ONLY_COMMANDS), {0x10, 0x23, 0x2A, 0x2B})
        self.assertEqual(
            protocol.READ_ONLY_COMMANDS[0x2A],
            protocol.CommandSpec(
                0x2A,
                bytes.fromhex("2a 00"),
                protocol.WriteMode.WITH_RESPONSE,
                5,
            ),
        )
        self.assertEqual(
            protocol.READ_ONLY_COMMANDS[0x2B],
            protocol.CommandSpec(
                0x2B,
                bytes.fromhex("2b 00 00 00 00"),
                protocol.WriteMode.WITH_RESPONSE,
                25,
            ),
        )


class ModelQueryTests(unittest.TestCase):
    def test_parses_positive_model_count_from_validated_frame(self):
        self.assertEqual(
            protocol.parse_model_count(bytes.fromhex("0e 03 2a 00 01")),
            1,
        )

    def test_rejects_zero_model_count(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_model_count(bytes.fromhex("0e 03 2a 00 00"))

    def test_parses_nul_terminated_model_identity_from_offset_six(self):
        frame = bytes.fromhex(
            "0e 17 2b 00 00 00 42 30 37 37 54 5f 55 53 5f 31 33 00"
            " 00 00 00 00 00 00 00"
        )

        self.assertEqual(protocol.parse_model_info(frame), "B077T_US_13")

    def test_rejects_model_identity_without_nul_in_twelve_byte_field(self):
        frame = bytes.fromhex(
            "0e 17 2b 00 00 00 42 30 37 37 54 31 32 33 34 35 36 37"
            " 00 00 00 00 00 00 00"
        )

        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_model_info(frame)

    def test_rejects_invalid_empty_and_wrong_family_model_identity(self):
        frames = (
            bytes.fromhex(
                "0e 17 2b 00 00 00 ff 00 00 00 00 00 00 00 00 00 00 00"
                " 00 00 00 00 00 00 00"
            ),
            bytes.fromhex(
                "0e 17 2b 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
                " 00 00 00 00 00 00 00"
            ),
            bytes.fromhex(
                "0e 17 2b 00 00 00 4f 54 48 45 52 00 00 00 00 00 00 00"
                " 00 00 00 00 00 00 00"
            ),
        )

        for frame in frames:
            with self.subTest(frame=frame), self.assertRaises(protocol.ProtocolError):
                protocol.parse_model_info(frame)


class FirmwareInfoTests(unittest.TestCase):
    def test_parses_otautility_fw_info_offsets_and_endianness(self):
        frame = bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")

        info = protocol.parse_firmware_info(frame)

        self.assertEqual(info.version, "1.0")
        self.assertEqual(info.checksum, 0x6162)
        self.assertEqual(info.raw, frame)

    def test_rejects_wrong_opcode_length_and_status(self):
        frames = (
            bytes.fromhex("0e 08 23 00 31 2e 30 00 62 61"),
            bytes.fromhex("0e 09 22 00 31 2e 30 00 00 62 61"),
            bytes.fromhex("0e 09 23 01 31 2e 30 00 00 62 61"),
        )

        for frame in frames:
            with self.subTest(frame=frame), self.assertRaises(protocol.ProtocolError):
                protocol.parse_firmware_info(frame)


if __name__ == "__main__":
    unittest.main()
