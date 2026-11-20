from pathlib import Path

from cosyvoice.service.synthesis import LANGUAGES, ROOT, Synthesis
from cosyvoice.service.tables import open_excel, sentence_rows

EXCEL_PATH = ROOT / "添加力量训练记录测试用例_中文_修正左右手_批量测试.xlsx"
LANGUAGE = "zh"
OUTPUT_DIR = ROOT / "data" / "excel_output"
SHEETS = []  # 例如 ["常规用例", "动作重复"]。留空处理全部工作表。


def main():
    if LANGUAGE.lower() not in LANGUAGES:
        raise ValueError("LANGUAGE 须为 zh 或 en")
    excel_path = Path(EXCEL_PATH)
    if not excel_path.is_file():
        raise FileNotFoundError(f"找不到 Excel 文件，请设置 EXCEL_PATH：{excel_path}")
    with open_excel(excel_path) as workbook:
        missing = set(SHEETS).difference(workbook.sheetnames)
        if missing:
            raise ValueError(f"找不到工作表：{', '.join(sorted(missing))}")
        modules = {}
        for sheet in workbook.worksheets:
            if SHEETS and sheet.title not in SHEETS:
                continue
            if sheet.title in {".", ".."} or "/" in sheet.title or "\\" in sheet.title:
                raise ValueError(f"工作表名无法用作输出目录：{sheet.title}")
            modules[sheet.title] = list(sentence_rows(sheet.values, sheet.title, test_sheet=True))

    engine = Synthesis()
    voices = engine.local_voices(LANGUAGES[LANGUAGE.lower()])
    voice_index = 0
    for sheet_name, sentences in modules.items():
        audio_dir = Path(OUTPUT_DIR) / "audio" / sheet_name
        label_dir = Path(OUTPUT_DIR) / "labels" / sheet_name
        audio_dir.mkdir(parents=True, exist_ok=True)
        label_dir.mkdir(parents=True, exist_ok=True)
        for number, (text, _, location) in enumerate(sentences, start=1):
            stem = f"{number:06d}"
            try:
                wav = engine.wav(text, voices[voice_index % len(voices)])
            except Exception as exc:
                raise RuntimeError(f"{location} 合成失败") from exc
            (audio_dir / f"{stem}.wav").write_bytes(wav)
            (label_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
            voice_index += 1
        print(f"{sheet_name} 已完成 {len(sentences)} 条")


if __name__ == "__main__":
    main()
