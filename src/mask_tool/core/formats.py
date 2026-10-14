"""脱敏支持格式常量（PPT 屏蔽入口，不删码）"""

# 允许脱敏的扩展名
# - xlsm：openpyxl keep_vba 路径，宏部件原样保留，
#   宏源码内容不参与脱敏——见 XlsxAdapter zip 预检告警
# - pdf：PyMuPDF redaction 原位替换（文本型 PDF；扫描件/加密件明确报错）
SUPPORTED_MASK_EXTS: frozenset = frozenset({".docx", ".xlsx", ".xlsm", ".pdf"})

# 屏蔽格式及原因：代码保留，但暂不开放脱敏
BLOCKED_EXTS: dict = {
    ".pptx": "PPT 脱敏暂未开放（跨 run 替换可靠性未达标准），仅保留代码",
}
