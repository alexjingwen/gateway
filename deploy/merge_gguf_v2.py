#!/usr/bin/env python3
"""合并 GGUF 分片文件 - 基于 llama.cpp gguf-split --merge 的正确逻辑

修复点：
1. 从所有分片读取 tensor 元数据（每个分片只含自己的 tensor）
2. tensor offset 重新计算（相对于合并后文件的 data 区域）
3. tensor 数据按顺序写入，每个 tensor 后加 padding 对齐到 32 字节
"""
import struct
import sys
import os

GGUF_DEFAULT_ALIGNMENT = 32

# GGML 类型: (blck_size, type_size)
GGML_TYPE_INFO = {
    0: (1, 4),      # F32
    1: (1, 2),      # F16
    2: (32, 18),    # Q4_0
    3: (32, 20),    # Q4_1
    6: (32, 22),    # Q5_0
    7: (32, 24),    # Q5_1
    8: (32, 34),    # Q8_0
    9: (32, 36),    # Q8_1
    10: (256, 84),  # Q2_K
    11: (256, 110), # Q3_K
    12: (256, 144), # Q4_K
    13: (256, 176), # Q5_K
    14: (256, 210), # Q6_K
    15: (256, 292), # Q8_K
    24: (1, 1),     # I8
    25: (1, 2),     # I16
    26: (1, 4),     # I32
    27: (1, 8),     # I64
    28: (1, 8),     # F64
    30: (1, 2),     # BF16
}

def ggml_nbytes(dims, ggml_type):
    blck_size, type_size = GGML_TYPE_INFO.get(ggml_type)
    nelements = 1
    for d in dims:
        nelements *= d
    return nelements // blck_size * type_size

def read_u32(f): return struct.unpack('<I', f.read(4))[0]
def read_u64(f): return struct.unpack('<Q', f.read(8))[0]
def read_i32(f): return struct.unpack('<i', f.read(4))[0]
def read_f32(f): return struct.unpack('<f', f.read(4))[0]
def read_bool(f): return struct.unpack('<B', f.read(1))[0]
def read_string(f):
    n = read_u64(f)
    return f.read(n).decode('utf-8')

def read_metadata_value(f, vtype):
    if vtype == 0: return read_bool(f)
    elif vtype == 1: return struct.unpack('<b', f.read(1))[0]
    elif vtype == 2: return struct.unpack('<H', f.read(2))[0]
    elif vtype == 3: return struct.unpack('<h', f.read(2))[0]
    elif vtype == 4: return read_u32(f)
    elif vtype == 5: return read_i32(f)
    elif vtype == 6: return read_f32(f)
    elif vtype == 7: return read_bool(f)
    elif vtype == 8: return read_string(f)
    elif vtype == 9:
        atype = read_u32(f)
        alen = read_u64(f)
        arr = []
        for _ in range(alen):
            if atype == 4: arr.append(read_u32(f))
            elif atype == 5: arr.append(read_i32(f))
            elif atype == 8: arr.append(read_string(f))
            elif atype == 6: arr.append(read_f32(f))
            elif atype in (0,1,7): arr.append(read_bool(f))
            elif atype in (2,3): arr.append(struct.unpack('<H', f.read(2))[0])
            else: raise ValueError(f"未知数组类型: {atype}")
        return ('ARRAY', atype, arr)
    else: raise ValueError(f"未知值类型: {vtype}")

def write_metadata_value(f, vtype, value):
    if vtype == 0: f.write(struct.pack('<B', value))
    elif vtype == 1: f.write(struct.pack('<b', value))
    elif vtype == 2: f.write(struct.pack('<H', value))
    elif vtype == 3: f.write(struct.pack('<h', value))
    elif vtype == 4: f.write(struct.pack('<I', value))
    elif vtype == 5: f.write(struct.pack('<i', value))
    elif vtype == 6: f.write(struct.pack('<f', value))
    elif vtype == 7: f.write(struct.pack('<B', value))
    elif vtype == 8:
        b = value.encode('utf-8')
        f.write(struct.pack('<Q', len(b)))
        f.write(b)
    elif vtype == 9:
        _, atype, arr = value
        f.write(struct.pack('<I', atype))
        f.write(struct.pack('<Q', len(arr)))
        for v in arr:
            if atype == 4: f.write(struct.pack('<I', v))
            elif atype == 5: f.write(struct.pack('<i', v))
            elif atype == 8:
                b = v.encode('utf-8')
                f.write(struct.pack('<Q', len(b)))
                f.write(b)
            elif atype == 6: f.write(struct.pack('<f', v))
            elif atype in (0,1,7): f.write(struct.pack('<B', v))
            elif atype in (2,3): f.write(struct.pack('<H', v))
    else: raise ValueError(f"未知值类型: {vtype}")

def parse_gguf_header(f):
    magic = f.read(4)
    if magic != b'GGUF':
        raise ValueError(f"不是 GGUF 文件: {magic}")
    version = read_u32(f)
    tensor_count = read_u64(f)
    metadata_kv_count = read_u64(f)

    metadata = []
    for _ in range(metadata_kv_count):
        key = read_string(f)
        vtype = read_u32(f)
        value = read_metadata_value(f, vtype)
        metadata.append((key, vtype, value))

    tensors = []
    for _ in range(tensor_count):
        name = read_string(f)
        n_dims = read_u32(f)
        dims = [read_u64(f) for _ in range(n_dims)]
        ggml_type = read_u32(f)
        offset = read_u64(f)
        tensors.append((name, dims, ggml_type, offset))

    pos = f.tell()
    aligned = (pos + GGUF_DEFAULT_ALIGNMENT - 1) & ~(GGUF_DEFAULT_ALIGNMENT - 1)

    return version, tensor_count, metadata, tensors, aligned

def merge(shards_dir, output_path):
    shards = sorted([f for f in os.listdir(shards_dir) if f.endswith('.gguf')])
    print(f"找到 {len(shards)} 个分片:")
    for s in shards:
        size = os.path.getsize(os.path.join(shards_dir, s))
        print(f"  {s}: {size/1024/1024/1024:.2f} GB")

    # 1. 读取所有分片，收集元数据和 tensor 列表
    all_metadata = None
    all_version = None
    all_tensors = []  # [(name, dims, ggml_type, shard_idx, offset_in_shard), ...]
    shard_info = []   # [(path, data_offset), ...]

    for i, shard_name in enumerate(shards):
        shard_path = os.path.join(shards_dir, shard_name)
        with open(shard_path, 'rb') as f:
            version, tensor_count, metadata, tensors, data_offset = parse_gguf_header(f)
            print(f"\n分片 {i+1} ({shard_name}): GGUF v{version}, tensors={tensor_count}, metadata={len(metadata)}, data_offset={data_offset}")

            if i == 0:
                all_version = version
                all_metadata = metadata
            shard_info.append((shard_path, data_offset))
            for t in tensors:
                all_tensors.append((t[0], t[1], t[2], i, t[3]))

    print(f"\n总计 tensor 数: {len(all_tensors)}")

    # 2. 过滤 split.* 元数据
    filtered_metadata = [(k, v, val) for k, v, val in all_metadata if not k.startswith('split.')]
    print(f"过滤后元数据: {len(filtered_metadata)} (移除了 {len(all_metadata) - len(filtered_metadata)} 个 split.* 字段)")

    # 3. 计算每个 tensor 的新 offset 和数据大小
    tensor_meta = []  # [(name, dims, ggml_type, new_offset, nbytes, shard_idx, offset_in_shard), ...]
    current_offset = 0
    total_data_size = 0
    for name, dims, ggml_type, shard_idx, offset_in_shard in all_tensors:
        nbytes = ggml_nbytes(dims, ggml_type)
        padded = (nbytes + GGUF_DEFAULT_ALIGNMENT - 1) & ~(GGUF_DEFAULT_ALIGNMENT - 1)
        tensor_meta.append((name, dims, ggml_type, current_offset, nbytes, shard_idx, offset_in_shard))
        current_offset += padded
        total_data_size += padded

    print(f"总 tensor 数据大小: {total_data_size/1024/1024/1024:.2f} GB")

    # 4. 写入合并文件
    print(f"\n写入合并文件: {output_path}")
    print("这可能需要几分钟...")

    with open(output_path, 'wb') as out:
        # 写入 header
        out.write(b'GGUF')
        out.write(struct.pack('<I', all_version))
        out.write(struct.pack('<Q', len(all_tensors)))
        out.write(struct.pack('<Q', len(filtered_metadata)))

        for key, vtype, value in filtered_metadata:
            key_bytes = key.encode('utf-8')
            out.write(struct.pack('<Q', len(key_bytes)))
            out.write(key_bytes)
            out.write(struct.pack('<I', vtype))
            write_metadata_value(out, vtype, value)

        for name, dims, ggml_type, new_offset, nbytes, shard_idx, offset_in_shard in tensor_meta:
            name_bytes = name.encode('utf-8')
            out.write(struct.pack('<Q', len(name_bytes)))
            out.write(name_bytes)
            out.write(struct.pack('<I', len(dims)))
            for d in dims:
                out.write(struct.pack('<Q', d))
            out.write(struct.pack('<I', ggml_type))
            out.write(struct.pack('<Q', new_offset))

        # 对齐到 GGUF_DEFAULT_ALIGNMENT
        pos = out.tell()
        aligned = (pos + GGUF_DEFAULT_ALIGNMENT - 1) & ~(GGUF_DEFAULT_ALIGNMENT - 1)
        out.write(b'\x00' * (aligned - pos))
        print(f"Header: {aligned} bytes")

        # 5. 写入 tensor 数据（按顺序，每个 tensor 后加 padding）
        for idx, (name, dims, ggml_type, new_offset, nbytes, shard_idx, offset_in_shard) in enumerate(tensor_meta):
            shard_path, data_offset = shard_info[shard_idx]
            file_offset = data_offset + offset_in_shard
            padded = (nbytes + GGUF_DEFAULT_ALIGNMENT - 1) & ~(GGUF_DEFAULT_ALIGNMENT - 1)

            with open(shard_path, 'rb') as f:
                f.seek(file_offset)
                remaining = nbytes
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024 * 64, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)

            # 写入 padding
            out.write(b'\x00' * (padded - nbytes))

            if (idx + 1) % 100 == 0 or idx == len(tensor_meta) - 1:
                print(f"  {idx+1}/{len(all_tensors)} tensors ({(idx+1)*100//len(all_tensors)}%)", flush=True)

    total = os.path.getsize(output_path)
    print(f"\n✅ 合并完成: {total/1024/1024/1024:.2f} GB")
    print(f"   tensor 数: {len(all_tensors)}")
    print(f"   元数据数: {len(filtered_metadata)}")

if __name__ == '__main__':
    shards_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/.ollama-gguf/qwen3.5-122b/Q4_K_M')
    output_path = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser('~/.ollama-gguf/qwen3.5-122b/Qwen3.5-122B-A10B-Q4_K_M-merged.gguf')
    merge(shards_dir, output_path)
