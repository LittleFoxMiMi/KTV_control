import os
import torch
import numpy as np
import ncnn
import librosa
import soundfile as sf
import warnings
import argparse
import subprocess
import sys

warnings.filterwarnings("ignore")

class STFT:
    def __init__(self, n_fft, hop_length, dim_f, device):
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.window = torch.hann_window(window_length=self.n_fft, periodic=True)
        self.dim_f = dim_f
        self.device = device

    def __call__(self, x):
        x = x.to(self.device)
        window = self.window.to(x.device)
        batch_dims = x.shape[:-2]
        c, t = x.shape[-2:]
        x = x.reshape([-1, t])
        x = torch.stft(x, n_fft=self.n_fft, hop_length=self.hop_length, window=window, center=True, return_complex=False)
        x = x.permute([0, 3, 1, 2])
        x = x.reshape([*batch_dims, c, 2, -1, x.shape[-1]]).reshape([*batch_dims, c * 2, -1, x.shape[-1]])
        return x[..., :self.dim_f, :]

    def inverse(self, x):
        x = x.to(self.device)
        window = self.window.to(x.device)
        batch_dims = x.shape[:-3]
        c, f, t = x.shape[-3:]
        n = self.n_fft // 2 + 1
        f_pad = torch.zeros([*batch_dims, c, n - f, t]).to(x.device)
        x = torch.cat([x, f_pad], -2)
        x = x.reshape([*batch_dims, c // 2, 2, n, t]).reshape([-1, 2, n, t])
        x = x.permute([0, 2, 3, 1])
        x = x[..., 0] + x[..., 1] * 1.j
        x = torch.istft(x, n_fft=self.n_fft, hop_length=self.hop_length, window=window, center=True)
        x = x.reshape([*batch_dims, 2, -1])
        return x

class MDXNetKVocalRemover:
    def __init__(self, param_path: str, bin_path: str, use_gpu: bool = False, gpu_index: int = 0, 
                 use_denoise: bool = False, batch_size: int = 1, segment_size: int = 256):
        self.param_path = param_path
        self.bin_path = bin_path
        self.use_denoise = use_denoise
        self.batch_size = batch_size
        
        # Device setup (CPU for NCNN, GPU support via NCNN configuration)
        if use_gpu:
            print(f"Using GPU (NCNN)")
            # NCNN GPU support can be enabled by setting num_threads appropriately
            self.use_gpu = True
            self.gpu_index = gpu_index
        else:
            print(f"Using CPU (NCNN)")
            self.use_gpu = False
            self.gpu_index = 0

        # UVR_MDXNET_KARA_2 configuration
        self.dim_f = 2048
        self.dim_t = segment_size  # Segment Size
        self.n_fft = 5120
        self.hop = 1024
        self.compensate = 1.065
        
        self.n_bins = self.n_fft // 2 + 1
        self.trim = self.n_fft // 2
        self.chunk_size = self.hop * (self.dim_t - 1)
        self.gen_size = self.chunk_size - 2 * self.trim
        
        # STFT processing on CPU
        self.stft = STFT(self.n_fft, self.hop, self.dim_f, torch.device('cpu'))
        
        print(f"Loading NCNN model...")
        self._load_ncnn_model()

    def _load_ncnn_model(self):
        """Load NCNN model from param and bin files"""
        try:
            # Use context manager like in the original example
            self.net = ncnn.Net()
            
            # Disable ALL reduced-precision paths to prevent high-frequency noise
            self.net.opt.use_fp16_packed = False
            self.net.opt.use_fp16_storage = False
            self.net.opt.use_fp16_arithmetic = False
            self.net.opt.use_bf16_storage = False
            self.net.opt.use_packing_layout = False
            if hasattr(self.net.opt, 'use_shader_pack8'):
                self.net.opt.use_shader_pack8 = False
            if hasattr(self.net.opt, 'use_image_storage'):
                self.net.opt.use_image_storage = False
            
            if self.use_gpu:
                if ncnn.get_gpu_count() > 0:
                    self.net.opt.use_vulkan_compute = True
                    
                    if hasattr(self.net, 'set_vulkan_device'):
                        self.net.set_vulkan_device(self.gpu_index)
                else:
                    print("Warning: GPU requested but no Vulkan GPU found by NCNN.")
            
            # Load param and model
            self.net.load_param(self.param_path)
            self.net.load_model(self.bin_path)
            
            print(f"NCNN model loaded successfully")
        except Exception as e:
            print(f"Failed to load NCNN model: {e}")
            raise e

    def convert_to_wav(self, input_path):
        temp_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Temp', 'vocal'))
        os.makedirs(temp_dir, exist_ok=True)
        base_name = os.path.basename(input_path)
        temp_filename = f"{os.path.splitext(base_name)[0]}_temp.wav"
        temp_path = os.path.join(temp_dir, temp_filename)
        
        print(f"Converting input to WAV via ffmpeg: {temp_path}")
        
        cmd = [
            'ffmpeg', '-i', input_path, 
            '-vn',               # No video
            '-ar', '44100',      # Sample rate 44.1k
            '-ac', '2',          # Stereo
            '-f', 'wav',         # Format wav
            temp_path, '-y'      # Overwrite
        ]
        
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return temp_path
        except subprocess.CalledProcessError as e:
            print(f"Error during ffmpeg conversion: {e}")
            return None
        except FileNotFoundError:
            print("Error: ffmpeg command not found. Please ensure ffmpeg is installed and in your PATH.")
            return None

    def process_file(self, input_path: str, output_path: str, save_vocals: bool = False):
        input_path = os.path.abspath(input_path)
        output_path = os.path.abspath(output_path)
        
        if not os.path.exists(input_path):
            print(f"File not found: {input_path}")
            return

        # Pre-process with ffmpeg
        wav_input_path = self.convert_to_wav(input_path)
        if not wav_input_path:
            print("Failed to preprocess audio file.")
            return
            
        try:
            print(f"Loading audio: {wav_input_path}")
            # Load audio at 44.1kHz
            mix, sr = librosa.load(wav_input_path, mono=False, sr=44100)
            
            # Ensure stereo
            if mix.ndim == 1:
                mix = np.asfortranarray([mix, mix])
            
            print(f"Processing... (using denoise={self.use_denoise})")
            sys.stdout.flush()
            # For UVR_MDXNET_KARA_2, primary stem is Instrumental.
            instrumental = self.demix(mix)
            
            # Save Instrumental
            print(f"Saving instrumental to: {output_path}")
            sys.stdout.flush()
            sf.write(output_path, instrumental.T, 44100)

            if save_vocals:
                # Deriving Vocals
                vocals = mix - instrumental
                
                base, ext = os.path.splitext(output_path)
                vocal_path = f"{base}_vocal{ext}"

                # Save vocals
                print(f"Saving vocals to: {vocal_path}")
                sf.write(vocal_path, vocals.T, 44100)

            print("Done.")
            sys.stdout.flush()
            
        except Exception as e:
            print(f"\n[Error] Processing failed: {e}")
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            raise e
        finally:
            temp_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Temp', 'vocal'))
            if os.path.exists(temp_dir):
                print(f"Cleaning up vocal temp directory: {temp_dir}")
                for filename in os.listdir(temp_dir):
                    file_path = os.path.join(temp_dir, filename)
                    try:
                        if os.path.isfile(file_path) or os.path.islink(file_path):
                            os.unlink(file_path)
                    except Exception as e:
                        print(f"Warning: Could not remove {file_path}: {e}")

    def run_model(self, mix):
        # mix: (batch, 2, time)
        # Ensure input is on CPU for STFT
        mix = mix.to('cpu')
        
        spek = self.stft(mix)  # (batch, 4, dim_f, dim_t)
        
        # Zero out first 3 frequency bins (UVR logic)
        spek[:, :, :3, :] *= 0 
        
        if self.use_denoise:
            # Phase inversion ensemble
            spec_pred = -self._execute_ncnn(-spek) * 0.5 + self._execute_ncnn(spek) * 0.5
        else:
            spec_pred = self._execute_ncnn(spek)
        
        # spec_pred is numpy array from _execute_ncnn
        # STFT inverse on CPU
        return self.stft.inverse(torch.tensor(spec_pred)).cpu().detach().numpy()

    def _execute_ncnn(self, spek):
        """Execute NCNN model inference"""
        input_data = spek.cpu().numpy().astype(np.float32)

        try:
            # Input shape: (batch, 4, dim_f, dim_t)
            batch_size = input_data.shape[0]
            channels = input_data.shape[1]
            h = input_data.shape[2]
            w = input_data.shape[3]

            def process_single(input_single):
                # Ensure C-contiguous memory layout — ncnn.Mat(numpy) expects CHW contiguous
                input_single = np.ascontiguousarray(input_single, dtype=np.float32)

                with self.net.create_extractor() as ex:
                    mat_in = ncnn.Mat(input_single).clone()
                    ex.input("in0", mat_in)
                    ret, output_mat = ex.extract("out0")
                    if ret != 0:
                        print(f"Warning: NCNN extract returned {ret}")
                    out = np.array(output_mat, dtype=np.float32)
                    # ncnn Mat from 3D model output should already be (c, h, w)
                    if out.ndim == 1:
                        out = out.reshape(channels, h, w)
                    return out

            # NCNN 本身针对端侧设计，原生 Mat 在常规层不支持多维 Batch 计算。
            # 这里统一改为了顺序执行。
            # 一方面避免多线程提取器并发调用导致由于设备驱动限制造成的 Vulkan VK_ERROR_DEVICE_LOST (vkQueueSubmit failed -4) 崩溃；
            # 另一方面因为 NCNN 内部已经使用了 OpenMP/Vulkan 队列并发机制，使用 Python 外部多线程往往会导致缓存未命中或者竞争。
            output_blocks = []
            for i in range(batch_size):
                output_blocks.append(process_single(input_data[i]))
            
            # Stack back into batch: (batch, ...)
            output_data = np.stack(output_blocks, axis=0)
            return output_data
            
        except Exception as e:
            print(f"\n[Error] NCNN Execution Failed: {e}")
            print("Please ensure:")
            print("1. NCNN model files (.param and .bin) are valid and exist")
            print("2. The model input/output layer names match ('in0', 'out0')")
            print("3. Sufficient memory is available")
            import traceback
            traceback.print_exc()
            raise e

    def demix(self, mix):
        # mix shape: (2, samples)
        # Pad mixture
        pad = self.gen_size + self.trim - ((mix.shape[-1]) % self.gen_size)
        mixture = np.concatenate((np.zeros((2, self.trim), dtype='float32'), mix, np.zeros((2, pad), dtype='float32')), 1)

        overlap = 0.25
        step = int((1 - overlap) * self.chunk_size)
        
        result = np.zeros((1, 2, mixture.shape[-1]), dtype=np.float32)
        divider = np.zeros((1, 2, mixture.shape[-1]), dtype=np.float32)
        
        # Pre-process chunks info
        chunk_metadata = []
        batch_tensors = []
        
        total_chunks = 0
        for i in range(0, mixture.shape[-1], step):
            start = i
            end = min(i + self.chunk_size, mixture.shape[-1])
            chunk_size_actual = end - start
            
            window = np.hanning(chunk_size_actual)
            window = np.tile(window[None, None, :], (1, 2, 1))

            mix_part_ = mixture[:, start:end]
            if end != i + self.chunk_size:
                pad_size = (i + self.chunk_size) - end
                mix_part_ = np.concatenate((mix_part_, np.zeros((2, pad_size), dtype='float32')), axis=-1)
            
            batch_tensors.append(mix_part_)
            chunk_metadata.append((start, end, chunk_size_actual, window))
            total_chunks += 1

        print(f"Total chunks: {total_chunks}. Processing with batch size: {self.batch_size}")
        sys.stdout.flush()

        # Process batches
        for i in range(0, len(batch_tensors), self.batch_size):
            end_idx = min(i + self.batch_size, len(batch_tensors))
            current_batch_mix = np.array(batch_tensors[i:end_idx])
            
            # (Batch, 2, Chunk_Size)
            mix_part_tensor = torch.tensor(current_batch_mix, dtype=torch.float32)
            
            with torch.no_grad():
                # Expected output: (Batch, 2, Chunk_Size)
                batch_tar_waves = self.run_model(mix_part_tensor)
            
            # Progress update for external wrappers
            progress_val = int((end_idx / total_chunks) * 100)
            print(f"[PROGRESS] {progress_val}")
            sys.stdout.flush()

            for j, tar_waves in enumerate(batch_tar_waves):
                meta_idx = i + j
                start, end, chunk_size_actual, window = chunk_metadata[meta_idx]
                
                # Restore batch dimension (2, T) -> (1, 2, T) to match window shape
                tar_waves = tar_waves[None, ...]
                
                tar_waves[..., :chunk_size_actual] *= window 
                divider[..., start:end] += window
                result[..., start:end] += tar_waves[..., :end-start]

        tar_waves = result / divider
        source = tar_waves[:, :, self.trim:-self.trim]
        source = source[0]  # (2, samples)
        
        # Crop to original length
        source = source[:, :mix.shape[-1]]
        
        # Apply compensation
        source = source * self.compensate
        
        return source

    def __del__(self):
        """Cleanup NCNN net on deletion"""
        if hasattr(self, 'net'):
            try:
                self.net.clear()
            except:
                pass


if __name__ == "__main__":
    # Default model paths in this workspace - NCNN format
    DEFAULT_PARAM_PATH = r".\MDX_Net_Models\UVR_MDXNET_KARA_2.ncnn.param"
    DEFAULT_BIN_PATH = r".\MDX_Net_Models\UVR_MDXNET_KARA_2.ncnn.bin"
    
    parser = argparse.ArgumentParser(description="UVR MDX-Net Karaoke 2 Vocal Remover (NCNN)")
    parser.add_argument("-i", "--input", required=True, help="Path to input audio file")
    parser.add_argument("-o", "--output", help="Path to output instrumental file")
    parser.add_argument("--param", default=DEFAULT_PARAM_PATH, help="Path to NCNN param file")
    parser.add_argument("--bin", default=DEFAULT_BIN_PATH, help="Path to NCNN bin file")
    parser.add_argument("-gpu", "--gpu", action="store_true", help="Use GPU acceleration")
    parser.add_argument("-id", "--gpu_id", type=int, default=0, help="GPU Device ID (default: 0)")
    parser.add_argument("--denoise", action="store_true", help="Use denoise (phase inversion) - slower but potentially better quality")
    parser.add_argument("-vocal", "--save_vocals", action="store_true", help="Also save vocal track")
    parser.add_argument("-b", "--batch_size", type=int, default=2, help="Batch size for inference (default: 2)")
    parser.add_argument("-s", "--segment_size", type=int, default=256, help="Segment size (dim_t) for inference (default: 256)")
    
    args = parser.parse_args()
    
    output_file = args.output
    if not output_file:
        base, ext = os.path.splitext(args.input)
        output_file = f"{base}_instrumental{ext}"
    
    # Check model files exist
    if not os.path.exists(args.param):
        print(f"Error: NCNN param file not found at {args.param}")
        exit(1)
    if not os.path.exists(args.bin):
        print(f"Error: NCNN bin file not found at {args.bin}")
        exit(1)
        
    remover = MDXNetKVocalRemover(args.param, args.bin, use_gpu=args.gpu, gpu_index=0, 
                                  use_denoise=args.denoise, batch_size=args.batch_size, 
                                  segment_size=args.segment_size)
    remover.process_file(args.input, output_file, save_vocals=args.save_vocals)
