# MaaPiCli 使用说明

MaaPiCli 是 MaaFramework 提供的命令行版本，适合高级用户和服务器环境使用。

## 📦 一、下载与安装

### 1.1 下载

从 [GitHub Releases](https://github.com/KhazixW2/MAAGC/releases) 下载 PiCLI 版本：

```
maagc-win-x86_64-*-PiCLI.zip
```

### 1.2 解压

将下载的压缩包解压到任意目录，例如：

```
D:\MaaGC\
├── MaaPiCli.exe
├── python/
├── agent/
├── resource/
└── config/
```

### 1.3 验证安装

打开命令行（CMD 或 PowerShell），进入安装目录：

```bash
cd D:\MaaGC
.\MaaPiCli.exe --version
```

如果显示版本信息，说明安装成功。

---

## ⚙️ 二、配置

### 2.1 配置文件位置

配置文件位于 `config/maa_pi_config.json`

### 2.2 基础配置

编辑 `config/maa_pi_config.json`：

```json
{
  "controller": {
    "type": "Adb",
    "address": "127.0.0.1:5555"
  },
  "resource": "官服",
  "task": [
    {
      "name": "启动游戏",
      "option": []
    }
  ]
}
```

### 2.3 配置项说明

#### controller（控制器配置）

**ADB 控制器（模拟器）**：
```json
{
  "controller": {
    "type": "Adb",
    "address": "127.0.0.1:5555"
  }
}
```

**Win32 控制器（PC 端）**：
```json
{
  "controller": {
    "type": "Win32",
    "window_name": "游戏窗口标题"
  }
}
```

#### resource（资源包）

指定使用的资源包名称，通常为"官服"。

#### task（任务列表）

配置要执行的任务：

```json
{
  "task": [
    {
      "name": "启动游戏",
      "option": []
    },
    {
      "name": "推月",
      "option": []
    }
  ]
}
```

---

## 🚀 三、使用方式

### 3.1 直接运行

使用默认配置运行：

```bash
.\MaaPiCli.exe
```

### 3.2 指定配置文件

使用自定义配置文件：

```bash
.\MaaPiCli.exe --config config\custom_config.json
```

### 3.3 执行单个任务

通过命令行参数执行特定任务：

```bash
.\MaaPiCli.exe --task "启动游戏"
```

### 3.4 查看帮助

```bash
.\MaaPiCli.exe --help
```

---

## 📝 四、高级用法

### 4.1 多配置文件管理

可以为不同场景创建多个配置文件：

```
config/
├── daily_config.json      # 日常任务配置
├── monthly_config.json    # 月度任务配置
└── event_config.json      # 活动配置
```

切换配置：

```bash
.\MaaPiCli.exe --config config\daily_config.json
```

### 4.2 批处理脚本

创建批处理文件自动执行任务：

**daily_tasks.bat**：
```batch
@echo off
cd /d %~dp0
echo Starting daily tasks...
.\MaaPiCli.exe --task "启动游戏"
timeout /t 5
.\MaaPiCli.exe --task "推月"
.\MaaPiCli.exe --task "关闭游戏"
echo Tasks completed.
pause
```

### 4.3 配合任务计划程序

**Windows 任务计划程序**：

1. 打开"任务计划程序"
2. 点击"创建基本任务"
3. 设置任务名称（如"MaaGC 日常任务"）
4. 设置触发器（如每天上午 10:00）
5. 操作选择"启动程序"
6. 程序/脚本：`D:\MaaGC\MaaPiCli.exe`
7. 添加参数：`--config config\daily_config.json`
8. 完成创建

### 4.4 日志查看

实时查看日志：

**PowerShell**：
```powershell
Get-Content .\debug\maa.log -Wait -Tail 50
```

**CMD**：
```cmd
type debug\maa.log
```

---

## 🔧 五、配置示例

### 示例 1：模拟器日常任务

```json
{
  "controller": {
    "type": "Adb",
    "address": "127.0.0.1:5555"
  },
  "resource": "官服",
  "task": [
    {
      "name": "启动游戏",
      "option": []
    },
    {
      "name": "推月",
      "option": []
    },
    {
      "name": "关闭游戏",
      "option": []
    }
  ]
}
```

### 示例 2：PC 端任务

```json
{
  "controller": {
    "type": "Win32",
    "window_name": "游戏窗口标题"
  },
  "resource": "官服",
  "task": [
    {
      "name": "推年",
      "option": []
    }
  ]
}
```

### 示例 3：多开配置

**配置 1（实例 1）**：
```json
{
  "controller": {
    "type": "Adb",
    "address": "127.0.0.1:5555"
  },
  "resource": "官服",
  "task": [
    {
      "name": "推月",
      "option": []
    }
  ]
}
```

**配置 2（实例 2）**：
```json
{
  "controller": {
    "type": "Adb",
    "address": "127.0.0.1:5556"
  },
  "resource": "官服",
  "task": [
    {
      "name": "推月",
      "option": []
    }
  ]
}
```

---

## ⚠️ 六、注意事项

### 6.1 使用环境

- **操作系统**：Windows 10/11
- **Python**：已内置，无需单独安装
- **权限**：建议以管理员身份运行

### 6.2 性能优化

1. **关闭日志输出**（如不需要）
   - 修改配置文件设置日志级别

2. **降低执行速度**
   - 在配置中添加延迟参数

3. **避免同时运行多个实例**
   - 除非配置了多开

### 6.3 错误处理

**常见错误**：

1. **连接失败**
   ```
   Error: Failed to connect to device
   ```
   解决方案：检查设备连接和端口配置

2. **任务不存在**
   ```
   Error: Task 'XXX' not found
   ```
   解决方案：检查任务名称是否正确

3. **资源加载失败**
   ```
   Error: Failed to load resource
   ```
   解决方案：检查 resource 文件夹是否完整

---

## 🔗 七、相关文档

- [新手上路](./新手上路.md) - 快速开始指南
- [连接设置](./连接设置.md) - 设备连接配置
- [常见问题](./常见问题.md) - 问题排查方法

---

## 📞 获取帮助

如有问题，请通过以下方式反馈：

- **GitHub Issues**: [问题反馈](https://github.com/KhazixW2/MAAGC/issues)
- **GitHub Discussions**: [讨论区](https://github.com/KhazixW2/MAAGC/discussions)

---

**祝你使用愉快！** 🎉
