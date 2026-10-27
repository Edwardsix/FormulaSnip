# FormulaSnip 公式快贴 v1.0.0

首个公开版本：面向 Windows 的截图公式转写工具，支持自带云端识别 API Key。

## 功能

- Ctrl+Alt+M 快捷键框选截图，完成后自动识别。
- 支持 Mathpix 和 OpenAI 兼容视觉接口。
- 支持导入图片、粘贴图片和编辑 LaTeX。
- 支持复制 LaTeX、复制 Word 公式及导出可编辑公式的 DOCX。
- 支持本地 MathML 预览、会话历史和 Windows 用户密钥加密。

## 开始使用

下载本页 Assets 中的 `FormulaSnip-1.0.0-Windows-x64.zip`，完整解压，双击 `FormulaSnip.exe`，在设置中填写服务信息和自己的 Key。

不需要安装 Python；没有 Key 时可用“载入示例”体验复制和导出。

## 说明

- Windows 10 / 11，64 位；当前验证环境为 Windows 11。
- 云端识别可能收费，软件不附带免费额度。
- 识别结果需要人工核对；尚未使用真实用户 Key 验证识别准确率。
- Word 直接粘贴兼容性随版本而异；可改用 DOCX 导出。实际 Word 粘贴及视觉显示尚未实测。
- 开发测试：69 项通过、1 项剪贴板写入测试跳过；已检查便携版启动、截图、快捷键、加密及导出。

如果发现问题，欢迎提交 Issue；请不要上传 Key 或私人图片。
