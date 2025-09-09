class RecordingProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super(options);
    this.sampleRate = options.processorOptions.sampleRate;
    this.port.onmessage = (event) => {
      // Nothing to do here
    };
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    if (input.length > 0) {
      const pcmData = this.toFloat32ToPcm16(input[0]);
      this.port.postMessage(pcmData);
      console.log("Recording processor sent audio data");
    }
    return true;
  }

  toFloat32ToPcm16(buffer) {
    let pcm16 = new Int16Array(buffer.length);
    for (let i = 0; i < buffer.length; i++) {
      let s = Math.max(-1, Math.min(1, buffer[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return pcm16;
  }
}

registerProcessor('recording-processor', RecordingProcessor);
