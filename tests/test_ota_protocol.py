import unittest

from tools import ota_protocol as protocol


class ReadOnlyCommandTests(unittest.TestCase):
    def test_fw_info_command_has_vendor_write_mode_and_exact_payload(self):
        spec = protocol.READ_ONLY_COMMANDS[0x23]

        self.assertEqual(spec.request, bytes.fromhex("23 00"))
        self.assertIs(spec.write_mode, protocol.WriteMode.WITH_RESPONSE)

    def test_unverified_model_queries_remain_unavailable_and_not_allowlisted(self):
        self.assertEqual(protocol.ModelIdentity.UNAVAILABLE.value, "unavailable")
        self.assertEqual(set(protocol.READ_ONLY_COMMANDS), {0x10, 0x23})


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
