import { Mic } from 'lucide-react'
import Modal from './Modal'
import { Btn } from './ui'

interface Props {
  /** Whether the modal is open */
  open: boolean
  /** Close without navigating */
  onClose: () => void
  /** Navigate the user to the STT setting (Settings -> Voice) */
  onOpenSettings: () => void
}

/**
 * Shown when the user clicks the mic but server-side speech-to-text is
 * disabled. Recording while STT is off would capture audio that never gets
 * transcribed, so instead of silently failing we explain why and link to the
 * setting that turns it on.
 */
export default function VoiceDisabledModal({ open, onClose, onOpenSettings }: Props) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Turn on voice input"
      maxWidth={440}
      footer={
        <>
          <Btn onClick={onClose}>Not now</Btn>
          <Btn primary onClick={onOpenSettings}>Open settings</Btn>
        </>
      }
    >
      <div className="flex gap-3.5">
        <div className="shrink-0 w-10 h-10 rounded-lg bg-accent/15 text-accent flex items-center justify-center">
          <Mic size={20} />
        </div>
        <div className="text-[13px] text-text leading-relaxed">
          <p className="mb-2">
            Speech-to-text is not enabled yet, so the microphone cannot turn your voice into text.
          </p>
          <p className="text-muted">
            Enable it under <span className="text-text font-medium">Settings &rarr; Voice</span>, then click the mic to dictate into the message box.
          </p>
        </div>
      </div>
    </Modal>
  )
}
