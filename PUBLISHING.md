# 在 GitHub 公开 FormulaSnip

本目录是一份准备上传的源码副本，包含 README、截图、代码、测试、依赖许可及 `.gitignore`。公开仓库的上传和发布由项目作者自行完成。

## 1. 创建仓库

登录 GitHub，打开 https://github.com/new ：

- Repository name：`FormulaSnip`，也可以使用 `formula-snip`。
- Description：`快捷键截图识别数学公式，转换为 LaTeX 或可编辑 Word 公式的 Windows 工具，支持自带云端 API Key。`
- Visibility：选择 **Public**。
- README 和 `.gitignore`：本目录已提供，不需要让 GitHub 再生成。
- License：根据你的授权意愿选择。若暂未确定，可先不添加；不要在项目中声称已经采用某许可证。

点击 **Create repository**。

若愿意允许他人使用、修改、再分发及商用自己的代码，可了解 MIT 许可证；它要求保留版权及许可声明。最终选择由项目作者决定。GitHub 的项目许可与依赖许可是两件事，选择后还需更新 README 的“许可证”部分。

## 2. 上传源码

1. 先完整解压 `FormulaSnip-GitHub-ready.zip`。
2. 打开其中的 `FormulaSnip-GitHub` 文件夹。
3. 在仓库页面点击 **uploading an existing file**，或 **Add file → Upload files**。
4. 将该文件夹里的所有文件拖入上传框。文件列表顶层应能看到 `README.md`、`app.py`、`.gitignore`、`requirements.txt` 等。
5. 确认上传的是文件夹内容，而不是只上传一个源码 ZIP，或给仓库多套一层文件夹。
6. 提交说明填 `Initial public release`，提交到 `main`。按页面显示点击 **Commit changes**；若页面引导你创建分支，则先提交并合并相应 Pull request。

完成后，仓库主页会显示 README 和界面图。可在右侧 About 中添加 Topics：`latex`、`ocr`、`math`、`windows`、`python`、`tkinter`、`screenshot`、`word`。

上传网页时，请直接使用此整理目录，避免把本机整个工作目录一并拖入。

## 3. 发布可下载的软件

可执行程序通过 Releases 提供给普通用户：

1. 在仓库右侧点击 **Releases**，再点 **Create a new release / Draft a new release**。
2. 新建 Tag：`v1.0.0`，Target 选择已经上传源码的 `main`。
3. 标题：`FormulaSnip 公式快贴 v1.0.0`。
4. 将本目录 `RELEASE_NOTES.md` 的内容复制到发布说明框。
5. 把原先准备好的 **FormulaSnip-1.0.0-Windows-x64.zip** 拖入附件区域。
6. 核对后点击 **Publish release**。如果想先邀请测试者试用，可勾选 **This is a pre-release**。

附件应是便携版 ZIP，不是此次的 `FormulaSnip-GitHub-ready.zip`。GitHub 自动生成的 Source code ZIP 也不能替代包含 EXE 的便携包。

## 4. 公开范围

已整理的上传目录不包含真实 API Key、运行设置、依赖安装目录或构建临时文件。测试代码中的 synthetic/test-only 凭据是测试数据。

后续维护时，不要上传 `settings.json`、`.env`、真实密钥、缓存、临时预览及含私人信息的图片。`.gitignore` 可帮助 Git 忽略这些文件；网页手动上传时仍需自行选择文件。

不要把介绍写成“免费无限识别”“完全离线识别”“百分之百准确”或“Word 粘贴已全面验证”，这些均不符合当前版本的验证范围。

## 官方参考

- 创建仓库：https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository
- 上传文件：https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository
- 创建 Release：https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository?tool=webui
- 仓库许可证：https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository
- MIT 许可证说明：https://choosealicense.com/licenses/mit/
