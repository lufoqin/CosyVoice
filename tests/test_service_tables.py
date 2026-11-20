import unittest
from tempfile import SpooledTemporaryFile

from openpyxl import Workbook

from cosyvoice.service.tables import batch_entries


class UploadedTableTests(unittest.TestCase):
    def test_csv_upload_with_optional_ids(self):
        with SpooledTemporaryFile() as upload:
            upload.write("\ufeff文本,音频编号\r\n第一条,\r\n第二条,EX-02\r\n第三条,\r\n".encode("utf-8"))
            upload.seek(0)
            self.assertEqual(batch_entries(upload, "input.csv"), [
                ("000001.wav", "第一条"), ("EX-02.wav", "第二条"), ("000003.wav", "第三条"),
            ])

    def test_excel_upload_with_multiple_sheets(self):
        book = Workbook()
        book.active.append(["测试语句"])
        book.active.append(["第一条"])
        sheet = book.create_sheet("第二个模块")
        sheet.append(["文本", "音频编号"])
        sheet.append(["第二条", "000009"])
        with SpooledTemporaryFile() as upload:
            book.save(upload)
            upload.seek(0)
            self.assertEqual(batch_entries(upload, "input.xlsx"), [
                ("000001.wav", "第一条"), ("000009.wav", "第二条"),
            ])

    def test_duplicate_auto_generated_id_is_rejected(self):
        with SpooledTemporaryFile() as upload:
            upload.write("文本,音频编号\n第一条,000002\n第二条,\n".encode("utf-8"))
            upload.seek(0)
            with self.assertRaisesRegex(ValueError, "音频文件名重复"):
                batch_entries(upload, "input.csv")
