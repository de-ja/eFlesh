<h1 align="center" style="font-size: 2.0em; font-weight: bold; margin-bottom: 0; border: none; border-bottom: none;">eFlesh: Highly customizable Magnetic Touch Sensing using Cut-Cell Miscrostructures</h1>

##### <p align="center"> [Venkatesh Pattabiraman](https://venkyp.com), [Zizhou Huang](https://huangzizhou.github.io/), [Daniele Panozzo](https://cims.nyu.edu/gcl/daniele.html), [Denis Zorin](https://cims.nyu.edu/gcl/denis.html), [Lerrel Pinto](https://www.lerrelpinto.com/) and [Raunaq Bhirangi](https://raunaqbhirangi.github.io/)</p>
##### <p align="center"> New York University </p>

<!-- <p align="center">
  <img src="assets/eflesh.gif">
 </p> -->

#####
<div align="center">
    <a href="https://e-flesh.com"><img src="https://img.shields.io/static/v1?label=Project%20Page&message=Website&color=blue"></a> &ensp;
    <a href="https://arxiv.org/abs/2506.09994"><img src="https://img.shields.io/static/v1?label=Paper&message=Arxiv&color=red"></a> &ensp; 
    <a href="https://github.com/notvenky/eFlesh/blob/main/microstructure/README.md"><img src="https://img.shields.io/static/v1?label=CAD2eFlesh&message=Tool&color=lightblue"></a> &ensp;
    <a href="mailto:venkatesh.p@nyu.edu">
      <img src="https://img.shields.io/static/v1?label=Questions?&amp;message=Reach%20Out&amp;color=purple">
    </a>
    <!-- <a href="https://github.com/notvenky/eFlesh/tree/main/characterization/datasets"><img src="https://img.shields.io/static/v1?label=Characterization&message=Datasets&color=blue"></a> &ensp; -->
    
</div>

#####

## Getting Started
```
git clone --recurse-submodules https://github.com/notvenky/eFlesh.git
cd eFlesh
conda env create -f env.yml
conda activate eflesh
```

## Sensor Design
### Tested on Ubuntu 20.04, 22.04 and 24.04
System pre-requisites
```
sudo apt-get update && sudo apt-get install -y build-essential cmake libgmp-dev libmpfr-dev libcgal-dev libeigen3-dev libsuitesparse-dev libboost-all-dev
```
### Note: Running the following command as it is, uses 12 CPU nodes. You can customize by running ```./build.sh cpu_nodes=n``` where you can choose 'n' based on your system.
```
cd microstructure/microstructure_inflators && chmod +x build.sh && ./build.sh
```

You're now all set to use ```regular.ipynb``` and ```cut-cell.ipynb```to make your own eFlesh sensors, ensure to provide the correct paths against all marked palceholders - like path to your OBJ/STL fle.

## Sensor Fabrication

<p align="center">
  <img src="https://github.com/user-attachments/assets/de48d4cc-23c9-44f1-8513-785790dfbc8a" width="400" alt="fabrication_only">
</p>

### 3D Print with TPU

We slice the generated STL file with pouches, using [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) or [Bambu Studio](https://bambulab.com/en/download/studio) and 3D print it with [TPU 95A](https://www.amazon.com/Polymaker-Filament-Flexible-1-75mm-Cardboard/dp/B09KKRYHS6) on a Bambu Lab X1 Carbon 3D printer.

### Neodymium Magnets

We use [N52 neodymium magnets](https://www.mcmaster.com/products/magnets/magnets-2~/neodymium-magnets-7/) of dimensions: [1/8" thickness, 3/8" diameter](https://www.mcmaster.com/5862K104/) for the standard cuboidal instance and many of the medium-large form factor sensors. For the fingertips, we use N52 magnets of dimensions [1/16" thickness, 3/16" diameter](https://www.mcmaster.com/5862K139/). According to the user's requirements, the magnet pouches can be easily tweaked, and so magnets of [any dimensions](https://www.mcmaster.com/products/magnets/magnets-2~/neodymium-magnets-7/) can be used.

### Hall Sensors / Magnetometers

Please upload the arduino code located in ```arduino/eflesh_cmd_stream/eflesh_cmd_stream.ino``` to the qtPy. We use the rigid magnetometer PCBs used in Reskin and AnySkin. Details can be found in the [circuit section](https://github.com/raunaqbhirangi/reskin_sensor/tree/main/circuits) of [Reskin](https://reskin.dev/)'s repository.

## Sensor Characterization

<p align="center">
  <img src="https://github.com/user-attachments/assets/77d09c24-e864-44c0-94fd-ab1c16a869ef"
       width="400">
</p>

We characterize eFlesh's spatial resolution, normal force and shear force prediction accuracy through controlled experiments, The curated datasets can be found in ```characterization/datasets/```. For training, we use a simple two layered MLP with 128 nodes (```python train.py --mode <spatial/normal/shear> --folder /path/to/corresponding/dataset```).

## Slip Detection

<p align="center">
  <img src="https://github.com/user-attachments/assets/c4b08c86-2133-420a-a5de-988adfbe691d" width="400" alt="slip_detection">
</p>

We grasp different objects using the Hello Stretch Robot equipped with eFlesh, and tug at it to collect our dataset. The dataset can be found in ```slip_detection/data```, and the trained classifier is ```slip_detection/checkpoints/eflesh_linear.pkl```.

## Visuo-Tactile Policy Learnig

<p align="center">
  <img src="https://github.com/user-attachments/assets/3a67073b-86bd-47f2-8b17-40b094b6da39" width="400" alt="policies">
</p>

We perform four precise manipulation tasks, using the [Visuo-Skin](https://visuoskin.github.io) framework, achieving an average success rate of >90%. Representative videos of trained policies can be found on [our website](https://e-flesh.com/).

## Primary References
eFlesh draws upon these prior works:

1. [Cut-Cell Microstructures for Two-scale Structural Optimization](https://cims.nyu.edu/gcl/papers/2024-cutcells.pdf)
2. [Learning Precise, Contact-Rich Manipulation through Uncalibrated Tactile Skins](https://visuoskin.github.io)
3. [AnySkin: Plug-and-play Skin Sensing for Robotic Touch](https://any-skin.github.io)
4. [ReSkin: versatile, replaceable, lasting tactile skins](https://reskin.dev)

## Cite 
If you build on our work or find it useful, please cite it using the following bibtex
```
@article{pattabiraman2025eflesh,
  title={eFlesh: Highly customizable Magnetic Touch Sensing using Cut-Cell Microstructures},
  author={Pattabiraman, Venkatesh and Huang, Zizhou and Panozzo, Daniele and Zorin, Denis and Pinto, Lerrel and Bhirangi, Raunaq},
  journal={arXiv preprint arXiv:2506.09994},
  year={2025}
}
```

