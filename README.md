<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img alt="LOGO" src="https://cdn.jsdelivr.net/gh/MaaAssistantArknights/design@main/logo/maa-logo_512x512.png" width="256" height="256" />
</p>

<div align="center">

# MaaGC

**MaaGC** 是一款基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 开发的自动化助手工具。

[![GitHub release](https://img.shields.io/github/v/release/KhazixW2/MAAGC)](https://github.com/KhazixW2/MAAGC/releases)
[![GitHub stars](https://img.shields.io/github/stars/KhazixW2/MAAGC)](https://github.com/KhazixW2/MAAGC/stargazers)
[![GitHub license](https://img.shields.io/github/license/KhazixW2/MAAGC)](https://github.com/KhazixW2/MAAGC/blob/main/LICENSE)
[![MaaFramework](https://img.shields.io/badge/powered%20by-MaaFramework-blue)](https://github.com/MaaXYZ/MaaFramework)

</div>

## 📋 功能列表

- **启动/关闭游戏** - 自动化启动和关闭游戏客户端
- **推月** - 每月例行任务自动化处理
- **推年** - 每年例行任务自动化处理

更多功能正在开发中...

## 🚀 快速开始

### 下载方式

#### 方式一：GitHub Releases（推荐）

前往 [Releases 页面](https://github.com/KhazixW2/MAAGC/releases) 下载最新版本：

- **MFAA 版本** - 基于 Avalonia UI 的图形界面版本，适合普通用户
- **MXU 版本** - 基于 Tauri + React 的现代化界面版本
- **PiCLI 版本** - 命令行版本，适合高级用户

#### 方式二：Mirror 酱高速下载

使用 Mirror 酱可以快速下载和自动更新 MaaGC。

### 使用说明

#### 1. 连接配置

**模拟器连接**：
- 支持主流安卓模拟器（雷电、夜神、MuMu 等）
- 确保模拟器已开启 ADB 调试模式
- 分辨率建议设置为 1280x720 或更高

**PC 端连接**：
- 支持 Windows 平台 PC 端游戏
- 需要游戏窗口处于前台运行状态

#### 2. 首次使用

1. 下载并解压对应版本到任意目录
2. 运行 `maagc.exe`（MFAA/MXU 版本）或 `MaaPiCli.exe`（PiCLI 版本）
3. 按照引导完成连接配置
4. 选择需要执行的任务，点击开始

#### 3. 详细文档

- [新手上路](./docs/zh_cn/新手上路.md) - 使用前必看，快速配置和启动
- [功能介绍](./docs/zh_cn/功能介绍.md) - 详细的功能说明和使用技巧
- [连接设置](./docs/zh_cn/连接设置.md) - 模拟器、PC 端连接配置
- [常见问题](./docs/zh_cn/常见问题.md) - 遇到问题先看这里
- [MaaPiCli 使用说明](./docs/zh_cn/MaaPiCli 使用说明.md) - 命令行版使用指南

## 💻 版本说明

### MFAA 版本
- 基于 Avalonia UI 构建的跨平台图形界面
- 提供直观的任务配置和操作界面
- 适合大多数普通用户使用

### MXU 版本
- 基于 Tauri + React + TypeScript 构建的现代化界面
- 更流畅的用户体验和更美观的界面设计
- 推荐使用此版本获得最佳体验

### PiCLI 版本
- 命令行版本，无图形界面
- 适合服务器环境或高级用户
- 资源占用最低

## 🛠️ 开发相关

### 环境要求

- Python 3.8+
- Git
- MaaFramework

### 本地开发

1. **克隆项目**
   ```bash
   git clone https://github.com/KhazixW2/MAAGC.git
   cd MAAGC
   ```

2. **初始化子模块**
   ```bash
   git submodule update --init --recursive
   ```

3. **下载依赖**
   - 下载 [MaaFramework](https://github.com/MaaXYZ/MaaFramework/releases) 到 `deps` 目录
   - 下载 OCR 资源文件到 `assets/resource/model/ocr/` 目录

4. **运行测试**
   ```bash
   python agent/main.py
   ```

### 项目结构

```
MAAGC/
├── agent/              # Python 代理脚本
├── assets/             # 资源文件
│   ├── resource/       # 识别资源、任务配置
│   ├── config/         # 配置文件
│   └── interface.json  # 接口定义
├── tools/              # 工具脚本
└── docs/               # 文档
```

### 贡献指南

我们欢迎各种形式的贡献：

- 🐛 报告 Bug
- 💡 提出新功能建议
- 📝 改进文档
- 🔧 提交代码修复或新功能

请查看 [开发文档](./docs/zh_cn/开发指南.md) 了解更多详情。

## ❓ 常见问题

### 0. 运行时报错"应用程序无法正常启动"？

通常是缺少 Visual C++ 运行库，请安装 [vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe)。

### 1. OCR 识别失败，报错"Failed to load det or rec"？

请确保已正确下载 OCR 资源文件到 `assets/resource/model/ocr/` 目录，需要包含：
- `det.onnx`
- `rec.onnx`
- `keys.txt`

### 2. 无法连接到游戏？

- 检查模拟器是否已开启 ADB 调试
- 确认 ADB 端口配置正确（默认 5555）
- 尝试重启模拟器或重新连接

### 3. 任务执行异常？

- 检查游戏分辨率是否符合要求（建议 1280x720）
- 确保游戏窗口处于前台（PC 端）
- 查看日志文件 `debug/maa.log` 获取详细错误信息

更多问题请查看 [常见问题文档](./docs/zh_cn/常见问题.md) 或在 Issues 中提问。

## 📜 用户许可协议

使用本软件即表示您同意以下条款：

- 本软件按"现状"提供，不提供任何明示或暗示的保证
- 用户承诺已阅读并同意第三方应用（游戏）的用户协议
- 用户保证仅将本软件用于合法的测试目的
- 开发者不对用户使用本软件造成的任何损失承担责任

详细条款请查看 [用户许可协议](./assets/description.md)。

## 🙏 鸣谢

### 核心框架

- **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** - 基于图像识别的自动化黑盒测试框架

### UI 支持

- **[MFAAvalonia](https://github.com/MaaXYZ/MFAAvalonia)** - 基于 Avalonia UI 构建的通用 GUI 解决方案
- **[MXU](https://github.com/MistEO/MXU)** - 基于 Tauri + React + TypeScript 构建的现代化 GUI 客户端

### 感谢所有贡献者

[![Contributors](https://contrib.rocks/image?repo=KhazixW2/MAAGC&max=1000)](https://github.com/KhazixW2/MAAGC/graphs/contributors)

## 📞 联系我们

- **GitHub Issues**: [问题反馈](https://github.com/KhazixW2/MAAGC/issues)
- **讨论区**: [GitHub Discussions](https://github.com/KhazixW2/MAAGC/discussions)

## 📄 许可证

本项目采用 [MIT 许可证](LICENSE) 开源。

---

<div align="center">

**MaaGC** | 让自动化变得更简单

Made with ❤️ by MaaGC Team

</div>
