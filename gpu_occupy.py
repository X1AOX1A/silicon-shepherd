#!/usr/bin/env python3
import torch
import time
import argparse
import logging
import random
import os
import signal
import sys
import subprocess
from pathlib import Path
import yaml

# 配置文件和 PID 文件路径
SCRIPT_DIR = Path(__file__).parent
CONFIG_DIR = Path.home() / ".config" / "gpu_occupy"
PID_FILE = CONFIG_DIR / "occupy.pid"
LOG_FILE = CONFIG_DIR / "occupy.log"
CONFIG_YAML = SCRIPT_DIR / "config.yaml"

def setup_config_dir():
    """创建配置目录"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def load_config_defaults():
    """从YAML文件加载默认配置"""
    default_config = {
        'gpus': [0, 1, 2, 3],
        'memory': 38.0,
        'mem_threshold': 1.0,
        'wait_minutes': 5.0,
        'refresh_minutes': 1.0,
        'compute_min': 30.0,
        'sleep_min': 5.0,
        'no_compute': False,
        'log_level': 'INFO',
        'max_retries': 3
    }
    
    try:
        if CONFIG_YAML.exists():
            with open(CONFIG_YAML, 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f)
                if yaml_config:
                    default_config.update(yaml_config)
                    logging.debug(f"Loaded configuration from {CONFIG_YAML}")
    except Exception as e:
        logging.warning(f"Failed to load config from {CONFIG_YAML}: {e}, using built-in defaults")
    
    return default_config

def save_pid():
    """保存当前进程 PID"""
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

def get_pid():
    """获取已保存的 PID"""
    try:
        with open(PID_FILE, 'r') as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None

def is_process_running(pid):
    """检查进程是否在运行"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def kill_occupy_process():
    """终止 occupy 进程"""
    pid = get_pid()
    if pid and is_process_running(pid):
        # 检查当前进程状态
        process_phase = "unknown"
        if LOG_FILE.exists():
            try:
                with open(LOG_FILE, 'r') as f:
                    lines = f.readlines()
                    if lines:
                        recent_lines = [line.lower() for line in lines[-10:]]
                        if any('memory occupation started' in line for line in recent_lines):
                            process_phase = "occupation"
                        elif any('ready for' in line or 'wait timer' in line or 'need' in line and 'more minutes' in line for line in recent_lines):
                            process_phase = "waiting"
            except Exception:
                pass

        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)  # 等待进程优雅退出
            if is_process_running(pid):
                os.kill(pid, signal.SIGKILL)  # 强制终止

            # 根据阶段显示相应的取消信息
            if process_phase == "occupation":
                print(f"✅ Successfully stopped GPU occupation (PID: {pid})")
            elif process_phase == "waiting":
                print(f"✅ Successfully cancelled waiting phase (PID: {pid})")
            else:
                print(f"✅ Successfully stopped occupy process (PID: {pid})")
            return True
        except OSError:
            print(f"❌ Failed to stop process (PID: {pid})")
            return False
    else:
        print("ℹ️  No occupy process is currently running")
        return False

def cleanup_pid_file():
    """清理 PID 文件"""
    if PID_FILE.exists():
        PID_FILE.unlink()

def get_gpu_memory_info(gpu_index):
    """获取指定 GPU 的内存信息"""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits', f'--id={gpu_index}'],
            capture_output=True, text=True, check=True
        )
        memory_info = result.stdout.strip().split(', ')
        used_memory_mb = int(memory_info[0])
        total_memory_mb = int(memory_info[1])
        used_memory_gb = used_memory_mb / 1024
        total_memory_gb = total_memory_mb / 1024
        return used_memory_gb, total_memory_gb
    except (subprocess.CalledProcessError, ValueError, IndexError):
        return None, None

def check_gpu_ready_for_occupation(gpu_indexes, mem_threshold):
    """检查所有指定 GPU 是否准备好被占用（所有GPU的已使用内存都小于阈值）"""
    if mem_threshold <= 0:
        logging.info("Threshold <= 0, immediately ready for occupation")
        return True  # 如果阈值为 0 或负数，立即准备占用

    logging.info(f"Checking GPU readiness with used memory threshold {mem_threshold}GB")
    for gpu_index in gpu_indexes:
        used_memory_gb, total_memory_gb = get_gpu_memory_info(gpu_index)
        if used_memory_gb is None:
            logging.warning(f"Could not get memory info for GPU {gpu_index}, assuming ready for occupation")
            continue

        logging.info(f"GPU {gpu_index}: {used_memory_gb:.2f}GB used (threshold: {mem_threshold}GB)")
        if used_memory_gb >= mem_threshold:
            logging.info(f"GPU {gpu_index}: {used_memory_gb:.2f}GB used >= {mem_threshold}GB threshold (not ready)")
            return False

    logging.info("All GPUs have low usage, ready for occupation")
    return True

def signal_handler(signum, frame):
    """信号处理器，用于优雅退出"""
    logging.info("Received termination signal, cleaning up...")
    cleanup_pid_file()
    sys.exit(0)

def occupy_gpu_memory(gpu_indexes, memory_size, sleep_min, compute_min, compute=False, wait_minutes=0, mem_threshold=0, refresh_minutes=1):
    """GPU 内存和计算占用函数"""
    # 保存 PID（提前保存，以便在等待阶段也能被 stop 命令终止）
    save_pid()

    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler()
        ]
    )

    # 设置信号处理器
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # 记录初始信息
    logging.info(f"GPU indexes: {gpu_indexes}")
    logging.info(f"Memory size to occupy: {memory_size} GB")
    logging.info(f"Compute: {compute}")
    logging.info(f"Sleep interval: {sleep_min} minutes")
    if compute:
        logging.info(f"Compute duration: {compute_min} minutes")
    if mem_threshold > 0:
        logging.info(f"Memory threshold: {mem_threshold} GB")
        logging.info(f"Required wait time: {wait_minutes} minutes")
        logging.info(f"Check interval: {refresh_minutes} minutes")

    # 检查 GPU 准备状态并等待
    if mem_threshold > 0 and wait_minutes > 0:
        logging.info(f"Starting wait logic: threshold={mem_threshold}GB, wait_time={wait_minutes}min")
        ready_start_time = None
        while True:
            is_ready = check_gpu_ready_for_occupation(gpu_indexes, mem_threshold)
            logging.info(f"GPU ready check result: {is_ready}")
            
            if is_ready:
                if ready_start_time is None:
                    ready_start_time = time.time()
                    logging.info("All GPU(s) have low memory, starting wait timer...")

                wait_duration = (time.time() - ready_start_time) / 60  # 转换为分钟
                if wait_duration >= wait_minutes:
                    logging.info(f"GPU(s) have been ready for {wait_duration:.1f} minutes, starting occupation...")
                    break
                else:
                    remaining = wait_minutes - wait_duration
                    logging.info(f"GPU(s) ready for {wait_duration:.1f}/{wait_minutes} minutes, need {remaining:.1f} more minutes...")
            else:
                if ready_start_time is not None:
                    logging.info("GPU(s) no longer ready (some have high usage), resetting timer...")
                else:
                    logging.info("GPU(s) not ready (high usage detected), waiting...")
                ready_start_time = None

            logging.info(f"Sleeping for {refresh_minutes} minutes before next check...")
            time.sleep(refresh_minutes * 60)
    else:
        logging.info(f"Skipping wait logic: threshold={mem_threshold}, wait_time={wait_minutes}")

    # 创建 GPU 设备列表
    devices = [torch.device(f"cuda:{idx}") for idx in gpu_indexes]

    # 在每个 GPU 设备上创建张量来占用指定的内存大小
    tensors = []
    try:
        for device in devices:
            tensor = torch.zeros(int(memory_size * 1024 * 1024 * 1024 / 4), dtype=torch.float32, device=device)
            tensors.append(tensor)
            logging.info(f"Occupying {memory_size} GB of GPU {device.index} memory")
        logging.info("GPU memory occupation started. Use `occupy off` to stop.")

        if compute:
            logging.info("Occupying GPU utilization...")

        while True:
            if compute:
                logging.info(f"Starting compute cycle for {compute_min} minutes...")
                start_time = time.time()
                end_time = start_time + compute_min * 60  # 转换计算持续时间为秒

                while time.time() < end_time:
                    for tensor in tensors:
                        # 引入计算强度的随机波动
                        std = 28/len(gpu_indexes)
                        fluctuation_factor = random.uniform(0.5, std)  # 在基础强度的50%到150%之间波动
                        tensor.mul_(2).add_(1)
                        time.sleep(0.01 * fluctuation_factor)

                logging.info("Completed a compute cycle.")

            # 休眠直到下一个计算周期
            logging.info(f"Entering rest period for {sleep_min} minutes before the next compute cycle.")
            time.sleep(sleep_min * 60)

    except KeyboardInterrupt:
        logging.info("Received interrupt signal, cleaning up...")
    except Exception as e:
        logging.error(f"Error occurred: {e}")
    finally:
        cleanup_pid_file()
        logging.info("GPU occupation stopped.")

def start_occupy(args):
    """启动 GPU 占用"""
    # 检查是否已经在运行
    pid = get_pid()
    if pid and is_process_running(pid):
        print(f"⚠️  Occupy is already running (PID: {pid})")
        print(f"💡 Use 'occupy status' to check status or 'occupy off' to stop it first")
        return

    setup_config_dir()
    
    # 清空之前的日志文件
    if LOG_FILE.exists():
        LOG_FILE.unlink()
        print(f"🧹 Cleared previous log file")

    # 处理计算参数
    compute = not args.no_compute

    print(f"🚀 Starting GPU occupation...")
    print(f"")
    print(f"📊 Configuration:")
    print(f"  GPUs: {args.gpus}")
    print(f"  Memory: {args.memory} GB per GPU")
    if compute:
        print(f"  Compute: ✅ ON (compute: {args.compute_min}min, sleep: {args.sleep_min}min)")
    else:
        print(f"  Compute: ❌ OFF (memory only)")
    if args.mem_threshold > 0:
        print(f"  Memory threshold: {args.mem_threshold} GB (used memory)")
        print(f"  Required wait time: {args.wait_minutes} minutes")
        print(f"  Check interval: {args.refresh_minutes} minutes")
    else:
        print(f"  Memory threshold: ❌ Disabled (immediate occupation)")
    print(f"")
    print(f"📝 Log file: {LOG_FILE}")
    print(f"🔢 PID will be saved to: {PID_FILE}")
    print(f"")

    # 启动 GPU 占用
    occupy_gpu_memory(args.gpus, args.memory, args.sleep_min, args.compute_min, compute, args.wait_minutes, args.mem_threshold, args.refresh_minutes)

def stop_occupy():
    """停止 GPU 占用"""
    success = kill_occupy_process()
    if success:
        cleanup_pid_file()

def status_occupy():
    """显示 occupy 状态"""
    pid = get_pid()
    process_running = pid and is_process_running(pid)

    if process_running:
        print(f"🔄 Occupy is running (PID: {pid})")
        print(f"📝 Log file: {LOG_FILE}")

        # 显示最后几行日志
        if LOG_FILE.exists():
            try:
                with open(LOG_FILE, 'r') as f:
                    lines = f.readlines()
                    if lines:
                        print("\nLast 10 log entries:")
                        for line in lines[-10:]:
                            print(f"  {line.strip()}")

                        # 检查是否在等待阶段 - 更精确的检测
                        recent_lines = [line for line in lines[-3:]]  # 检查最近3行
                        last_line = lines[-1].lower() if lines else ""

                        # 如果最后一行包含等待相关的关键词，且没有"occupation started"或"stopped"
                        if ('ready for' in last_line or 'need' in last_line and 'more minutes' in last_line) and \
                           not any('occupation started' in line.lower() or 'stopped' in line.lower() for line in recent_lines[-3:]):
                            print("\n⏳ [Status: Currently in waiting phase - monitoring GPU usage]")
                            print("💡 Use 'occupy off' to cancel waiting and exit")
                        elif any('occupation started' in line.lower() for line in recent_lines):
                            print("\n🔥 [Status: Currently occupying GPU memory and compute]")
                            print("💡 Use 'occupy off' to stop occupation and exit")
            except Exception as e:
                print(f"Error reading log file: {e}")
    else:
        print("⭕ Occupy is not running")
        cleanup_pid_file()  # 清理可能存在的旧 PID 文件

def main():
    # 加载配置默认值
    config_defaults = load_config_defaults()
    
    parser = argparse.ArgumentParser(description="GPU Memory and Utilization Occupancy Control Script")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # on 命令
    on_parser = subparsers.add_parser('on', help='Start GPU occupation')
    on_parser.add_argument("--gpus", nargs="+", type=int, default=config_defaults['gpus'],
                          help=f"GPU indexes to use (default: {config_defaults['gpus']})")
    on_parser.add_argument("--memory", type=float, default=config_defaults['memory'],
                          help=f"Memory size to occupy in GB (default: {config_defaults['memory']})")
    on_parser.add_argument("--sleep_min", type=float, default=config_defaults['sleep_min'],
                          help=f"Sleep time between compute cycles in minutes (default: {config_defaults['sleep_min']})")
    on_parser.add_argument("--compute_min", type=float, default=config_defaults['compute_min'],
                          help=f"Duration of each compute cycle in minutes (default: {config_defaults['compute_min']})")
    on_parser.add_argument("--no_compute", action='store_true', default=config_defaults['no_compute'],
                          help=f"Disable compute workload (default: {config_defaults['no_compute']})")
    on_parser.add_argument("--wait_minutes", type=float, default=config_defaults['wait_minutes'],
                          help=f"Wait this many minutes after all GPUs have low memory before occupation starts (default: {config_defaults['wait_minutes']})")
    on_parser.add_argument("--mem_threshold", type=float, default=config_defaults['mem_threshold'],
                          help=f"Memory threshold in GB - occupy when all GPU used memory < threshold (default: {config_defaults['mem_threshold']})")
    on_parser.add_argument("--refresh_minutes", type=float, default=config_defaults['refresh_minutes'],
                          help=f"Check interval in minutes for GPU memory status (default: {config_defaults['refresh_minutes']})")
    # 设置计算默认值
    on_parser.set_defaults(compute=not config_defaults['no_compute'])

    # off 命令
    subparsers.add_parser('off', help='Stop GPU occupation')

    # status 命令
    subparsers.add_parser('status', help='Show occupation status')

    args = parser.parse_args()

    if args.command == 'on':
        start_occupy(args)
    elif args.command == 'off':
        stop_occupy()
    elif args.command == 'status':
        status_occupy()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
