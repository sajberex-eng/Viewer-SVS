// Эталон для сверки переноса MarrowQuant 2.0 (этап 11, раздел 13.8 ТЗ).
//
// Дословно повторяет шаги класса MarrowQuant из MarrowQuant2.0.groovy
// (github.com/Naveiras-Lab/MarrowQuant2.0, лицензия BSD-3, авторы — Naveiras-Lab
// и EPFL BIOP) через те же вызовы ImageJ, но без QuPath: область скана, ткань и
// артефакты приходят готовыми картинками. Так наш перенос на Python сверяется с
// оригиналом по каждой точке каждой маски.
//
// Вход (папка): rgb.png — область в разрешении расчёта; tissue.png и art.png —
// маски 0/255 (art.png может не быть). Аргументы: папка, размер точки в мкм,
// adipMin, adipMax (мкм²), minCir.
// Выход в ту же папку: ref_bone.png, ref_imv.png, ref_hemato.png, ref_adip.png,
// ref_candidates.png (жировые кандидаты после разделения и «расширения»),
// ref.json с площадями в точках, порогами и числом жировых клеток.
//
// Запуск — только в контейнере с Java и виртуальным экраном: команды ImageJ
// с параметрами идут через окна настроек, как в QuPath (tools/mq_reference/run.sh).

import ij.*;
import ij.gui.*;
import ij.measure.Calibration;
import ij.plugin.ImageCalculator;
import ij.plugin.filter.ThresholdToSelection;
import ij.plugin.frame.RoiManager;
import ij.process.*;
import sc.fiji.colourDeconvolution.StainMatrix;

import java.io.File;
import java.io.FileWriter;
import java.util.*;

public class MQReference {
    static ImageCalculator ic = new ImageCalculator();
    static StringBuilder log = new StringBuilder();

    public static void main(String[] args) throws Exception {
        String dir = args[0];
        double pw = Double.parseDouble(args[1]);
        double adipMin = Double.parseDouble(args[2]);
        double adipMax = Double.parseDouble(args[3]);
        double minCir = Double.parseDouble(args[4]);

        ImageJ ij = new ImageJ(ImageJ.NO_SHOW);   // в QuPath: ij.setVisible(true)
        step("start");

        ImagePlus image = IJ.openImage(dir + "/rgb.png");
        if (image.getType() != ImagePlus.COLOR_RGB) new ij.process.ImageConverter(image).convertToRGB();
        Calibration cal = image.getCalibration();
        cal.pixelWidth = pw; cal.pixelHeight = pw; cal.setUnit("um");

        Roi tissueRoi = roiFromMaskFile(dir + "/tissue.png");
        Roi artifactsRoi = new File(dir + "/art.png").exists() ? roiFromMaskFile(dir + "/art.png") : null;

        // Разложение окрасок, как colorDeconvolution(image, "H&E DAB")
        StainMatrix mt = new StainMatrix();
        mt.init("H&E DAB", 0.650, 0.704, 0.286, 0.072, 0.990, 0.105, 0.268, 0.570, 0.776);
        ImageStack[] stackList = mt.compute(false, true, image);
        ImagePlus[] deconvolved = new ImagePlus[3];
        for (int i = 0; i < 3; i++) deconvolved[i] = new ImagePlus(image.getTitle() + "-H&E DAB", stackList[i]);

        // ---------- boneIMVFinder ----------
        step("boneIMVFinder");
        ImagePlus boneIMVImage = ic.run("Subtract create", deconvolved[0], deconvolved[1]);
        ImagePlus varianceImage = boneIMVImage.duplicate();
        IJ.run(varianceImage, "32-bit", "");
        rangeOf("after_32bit", varianceImage.getProcessor());
        IJ.run(varianceImage, "Variance...", "radius=1");
        rangeOf("after_variance", varianceImage.getProcessor());
        IJ.saveAsTiff(varianceImage.duplicate(), dir + "/ref_var.tif");
        IJ.run(varianceImage, "Add...", "value=1");
        rangeOf("after_add", varianceImage.getProcessor());
        ImagePlus boneImage = ic.run("Divide create 32-bit", boneIMVImage, varianceImage);
        ImageProcessor boneProc = boneImage.getProcessor();
        boneProc.setRoi(tissueRoi);
        boneProc.setAutoThreshold("MaxEntropy dark no-reset");
        log.append("\"bone_min\": ").append(boneProc.getMin()).append(", \"bone_max\": ").append(boneProc.getMax())
           .append(", \"bone_lower\": ").append(boneProc.getMinThreshold()).append(",\n");
        ByteProcessor boneMaskProc = boneProc.createMask();
        ImagePlus boneMaskImage = new ImagePlus("Bone Mask", boneMaskProc);
        boneMaskImage.setCalibration(image.getCalibration());
        IJ.run(boneMaskImage, "Options...", "iterations=5 count=1 black pad do=Dilate");
        IJ.run(boneMaskImage, "Options...", "iterations=20 count=2 black pad do=Close");
        boneMaskProc = (ByteProcessor) boneMaskImage.getProcessor();
        boneMaskProc.setBackgroundValue(0);
        if (artifactsRoi != null) boneMaskProc.fill(artifactsRoi);
        boneMaskProc.fillOutside(tissueRoi);
        Roi boneRoi = getRoiFromMask(boneMaskImage);
        boneMaskImage.setRoi(boneRoi);

        ImageProcessor varianceProc = varianceImage.getProcessor();
        log.append("\"var_min\": ").append(varianceProc.getMin()).append(", \"var_max\": ").append(varianceProc.getMax()).append(",\n");
        varianceProc.fillOutside(tissueRoi);
        if (boneRoi != null) varianceProc.fill(boneRoi);
        varianceProc.setRoi(tissueRoi);
        varianceProc.setAutoThreshold("MaxEntropy dark no-reset");
        log.append("\"var_min_after\": ").append(varianceProc.getMin()).append(", \"var_max_after\": ").append(varianceProc.getMax())
           .append(", \"var_lower\": ").append(varianceProc.getMinThreshold()).append(",\n");
        ByteProcessor imvMaskProc = varianceProc.createMask();
        if (artifactsRoi != null) imvMaskProc.fill(artifactsRoi);
        ImagePlus imvMaskImage = new ImagePlus("IMV", imvMaskProc);

        // ---------- hematoFinder ----------
        step("hematoFinder");
        ImagePlus hematoImage = ic.run("Subtract create", deconvolved[2], deconvolved[0]);
        ImageProcessor hematoProc = hematoImage.getProcessor();
        hematoProc.smooth();
        hematoProc.setRoi(tissueRoi);
        hematoProc.setAutoThreshold("Default dark");
        log.append("\"hem_lower\": ").append(hematoProc.getMinThreshold()).append(",\n");
        ByteProcessor hematoMaskProc = hematoProc.createMask();
        ImagePlus hematoMaskImage = new ImagePlus("Hemato Mask", hematoMaskProc);
        hematoMaskImage.setCalibration(image.getCalibration());
        IJ.run(hematoMaskImage, "Options...", "iterations=5 count=2 black pad do=Dilate");
        hematoMaskProc = (ByteProcessor) hematoMaskImage.getProcessor();
        hematoMaskProc.fillOutside(tissueRoi);
        if (boneRoi != null) hematoMaskProc.fill(boneRoi);
        if (artifactsRoi != null) hematoMaskProc.fill(artifactsRoi);
        ic.run("Subtract", imvMaskImage, hematoMaskImage);

        // ---------- adipFinder ----------
        step("adipFinder");
        ImagePlus hsbImage = image.duplicate();
        new ij.process.ImageConverter(hsbImage).convertToHSB();
        ImagePlus adipImage = ic.run("Subtract create", deconvolved[2], deconvolved[0]);
        hsbImage.getStack().getProcessor(2).multiply(8);
        ic.run("Add", adipImage, new ImagePlus("Saturation", hsbImage.getStack().getProcessor(2)));
        ImageProcessor adipProc = adipImage.getProcessor();
        adipProc.setThreshold(0, 200, ImageProcessor.NO_LUT_UPDATE);
        ByteProcessor adipMaskProc = adipProc.createMask();
        adipMaskProc.fillOutside(tissueRoi);
        if (boneRoi != null) adipMaskProc.fill(boneRoi);
        adipMaskProc.setBackgroundValue(255);
        if (artifactsRoi != null) adipMaskProc.fill(artifactsRoi);
        adipMaskProc.setBackgroundValue(0);
        ImagePlus adipMaskImage = new ImagePlus("Adip Mask", adipMaskProc);
        adipMaskImage.setCalibration(image.getCalibration());
        save(adipMaskImage.getProcessor(), dir + "/ref_adip_raw.png");
        IJ.saveAsTiff(new ImagePlus("edm", new ij.plugin.filter.EDM().makeFloatEDM(adipMaskImage.getProcessor(), 0, false)), dir + "/ref_edm.tif");
        step("watershed");
        IJ.run(adipMaskImage, "Watershed", "");
        save(adipMaskImage.getProcessor(), dir + "/ref_adip_ws.png");
        IJ.run(adipMaskImage, "Options...", "iterations=50 count=5 pad do=Dilate");
        save(adipMaskImage.getProcessor(), dir + "/ref_candidates.png");
        IJ.run(adipMaskImage, "Invert LUT", "");
        log.append("\"black_background_at_particles\": ").append(Prefs.blackBackground)
           .append(", \"inverted_lut_at_particles\": ").append(adipMaskImage.isInvertedLut()).append(",\n");

        step("particles");
        ImagePlus adipMaskImage2 = filterAdipocytes(adipMaskImage, adipMin, adipMax, minCir);
        adipMaskImage2.unlock();
        Roi adipRoi = getRoiFromMask(adipMaskImage2);
        ic.run("Subtract", imvMaskImage, adipMaskImage2);
        ic.run("Subtract", hematoMaskImage, adipMaskImage2);
        ImagePlus adipFinal = adipMaskImage2.duplicate();

        // Число жировых клеток (getIndividualAdipocytes)
        step("Число жировых клеток (getIndividualAdipocytes)");
        RoiManager rm = RoiManager.getRoiManager();
        rm.reset();
        IJ.run(adipFinal, "Analyze Particles...", "add");
        int nAdip = rm.getCount();
        rm.reset();

        // Маски как в отчёте: выделение из маски, то есть точки 127–255
        save(maskOf(boneMaskImage), dir + "/ref_bone.png");
        save(maskOf(imvMaskImage), dir + "/ref_imv.png");
        save(maskOf(hematoMaskImage), dir + "/ref_hemato.png");
        save(maskOf(adipMaskImage2), dir + "/ref_adip.png");

        try (FileWriter w = new FileWriter(dir + "/ref.json")) {
            w.write("{\n" + log);
            w.write("\"tissue_px\": " + count(roiMask(tissueRoi, image)) + ",\n");
            w.write("\"art_px\": " + (artifactsRoi == null ? 0 : count(roiMask(artifactsRoi, image))) + ",\n");
            w.write("\"bone_px\": " + count(maskOf(boneMaskImage)) + ",\n");
            w.write("\"imv_px\": " + count(maskOf(imvMaskImage)) + ",\n");
            w.write("\"hemato_px\": " + count(maskOf(hematoMaskImage)) + ",\n");
            w.write("\"adip_px\": " + count(maskOf(adipMaskImage2)) + ",\n");
            w.write("\"n_adip\": " + nAdip + "\n}\n");
        }
        System.exit(0);
    }

    static void rangeOf(String name, ImageProcessor p) {
        ImageStatistics st = ImageStatistics.getStatistics(p, ij.measure.Measurements.MIN_MAX, null);
        log.append("\"" + name + "\": [").append(p.getMin()).append(", ").append(p.getMax())
           .append(", ").append(st.min).append(", ").append(st.max).append("],\n");
    }

    static void step(String name) {
        System.out.println(new java.util.Date() + " " + name);
        System.out.flush();
    }

    static ImagePlus filterAdipocytes(ImagePlus adipMask, double adipMin, double adipMax, double minCir) {
        IJ.run(adipMask, "Analyze Particles...", "size=" + adipMin + "-" + adipMax + " circularity=" + minCir
                + "-1.00 clear add show=Masks");
        ImagePlus mask = IJ.getImage();
        mask.hide();
        RoiManager rm = RoiManager.getRoiManager();
        List<Roi> rois = new ArrayList<>(Arrays.asList(rm.getRoisAsArray()));
        log.append("\"particles_after_size_circ\": ").append(rois.size()).append(",\n");
        double minRound = 0.36;
        List<Roi> round = new ArrayList<>();
        for (Roi r : rois) {
            ij.process.ImageStatistics stats = r.getStatistics();
            double rnd = (4 * stats.area) / (Math.PI * Math.pow(stats.major, 2));
            if (rnd > minRound) round.add(r);
        }
        log.append("\"particles_after_round\": ").append(round.size()).append(",\n");
        rm.reset();
        for (Roi r : round) rm.addRoi(r);
        IJ.setForegroundColor(0, 0, 0);
        IJ.run(mask, "Set...", "value=0");
        rm.runCommand(mask, "Fill");
        return mask;
    }

    static Roi roiFromMaskFile(String path) {
        ImagePlus m = IJ.openImage(path);
        ImageProcessor p = m.getProcessor().convertToByte(false);
        p.setThreshold(127, 255, ImageProcessor.NO_LUT_UPDATE);
        return new ThresholdToSelection().convert(p);
    }

    static Roi getRoiFromMask(ImagePlus image) {
        ImageProcessor proc = image.getProcessor();
        proc.setThreshold(127, 255, ImageProcessor.NO_LUT_UPDATE);
        return new ThresholdToSelection().convert(proc);
    }

    // Маска выделения, которое getRoiFromMask даёт из картинки: те же точки 127–255
    static ByteProcessor maskOf(ImagePlus image) {
        Roi roi = getRoiFromMask(image);
        return roiMask(roi, image);
    }

    static ByteProcessor roiMask(Roi roi, ImagePlus image) {
        ByteProcessor out = new ByteProcessor(image.getWidth(), image.getHeight());
        if (roi == null) return out;
        out.setColor(255);
        out.fill(roi);
        return out;
    }

    static long count(ImageProcessor p) {
        long n = 0;
        for (int i = 0; i < p.getPixelCount(); i++) if (p.get(i) != 0) n++;
        return n;
    }

    static void save(ImageProcessor p, String path) {
        ByteProcessor b = (ByteProcessor) p.duplicate().convertToByte(false);
        b.setColorModel(null);
        ImagePlus out = new ImagePlus("m", b);
        if (p.isInvertedLut()) out.getProcessor().invertLut();
        IJ.saveAs(new ImagePlus("m", b), "PNG", path);
    }
}
