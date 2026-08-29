import { registerInterpolationStrategy } from '@soundtouchjs/core'
import { registerLinearStrategy } from '@soundtouchjs/interpolation-strategy-linear'
import { SoundTouchProcessorBase, STANDARD_PARAMETER_DESCRIPTORS } from '@soundtouchjs/worklet-base'

const PROCESSOR_NAME = 'soundtouch-processor'
const PROCESSOR_LABEL = '[LowCostSoundTouchProcessor]'

registerLinearStrategy({ registerInterpolationStrategy })

class LowCostSoundTouchProcessor extends SoundTouchProcessorBase {
  static get parameterDescriptors() {
    return STANDARD_PARAMETER_DESCRIPTORS
  }

  constructor(options) {
    const requestedStrategy = options?.processorOptions?.interpolationStrategy || 'linear'
    super(PROCESSOR_LABEL, {
      sampleRate,
      sampleBufferType: options?.processorOptions?.sampleBufferType || 'circular',
      interpolationStrategy: SoundTouchProcessorBase.resolveStrategy(requestedStrategy, PROCESSOR_LABEL)
    })
  }

  onProcessComplete(result) {
    if (this._blockCount % 100 !== 0) return
    this.port.postMessage({
      type: 'metrics',
      framesBuffered: result.available,
      underrunCount: this._underrunCount,
      blockCount: this._blockCount,
      outputRms: result.outputRms,
      outputPeak: result.outputPeak
    })
  }
}

registerProcessor(PROCESSOR_NAME, LowCostSoundTouchProcessor)
