import { AnimatePresence, motion } from "framer-motion";
import { FileImage, UploadCloud } from "lucide-react";
import { useRef, useState } from "react";
import styles from "./Dropzone.module.css";

export function Dropzone({
  onFile,
  fileName,
}: {
  onFile: (file: File) => void;
  fileName: string | null;
}) {
  const [hot, setHot] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div>
      <div
        className={`${styles.drop} ${hot ? styles.hot : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragEnter={(e) => {
          e.preventDefault();
          setHot(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setHot(true);
        }}
        onDragLeave={(e) => {
          e.preventDefault();
          setHot(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setHot(false);
          const f = e.dataTransfer.files[0];
          if (f) onFile(f);
        }}
      >
        <div className={styles.icon}>
          <UploadCloud size={20} strokeWidth={2} />
        </div>
        <p className={styles.big}>Drop a page here, or click to choose</p>
        <p className={styles.hint}>JPG or PNG · PDF if poppler is installed</p>
        <input
          ref={inputRef}
          type="file"
          accept=".jpg,.jpeg,.png,.pdf"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onFile(f);
            e.target.value = "";
          }}
        />
      </div>
      <AnimatePresence>
        {fileName && (
          <motion.div
            initial={{ opacity: 0, y: -6, height: 0 }}
            animate={{ opacity: 1, y: 0, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.18 }}
          >
            <span className={styles.file}>
              <FileImage size={13} /> {fileName}
            </span>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
