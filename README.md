# champs-scalar-coupling
### Predicting Molecular Properties with ShrinkSchNet

This project was created as part of the Kaggle competition [Predicting Molecular Properties](https://www.kaggle.com/c/champs-scalar-coupling) where it achieved a Top 2% ranking (46/2749).
The goal of the competition was to predict the scalar coupling between atom pairs for a diverse set of molecules. 
The model **"ShrinkSchNet"** is an extension of [SchNet](https://arxiv.org/abs/1706.08566)] ([repository](https://github.com/atomistic-machine-learning/schnetpack)). It implements
[edge updates](https://arxiv.org/abs/1806.03146) and a "shrinking layer" that gradually decreases the cutoff radius for interactions between the atom pair of interest and the remaining atoms inside the molecule. The readout function uses a permutation-invariant GRU over the coupled atom pair instead of a global pooling (as implemented in schnetpack).

`create_db.ipynb` converts the competition data into ASE databases that can be read by schnetpack.

`shrinkschnet.py` implements the modified model

`schnetpack_coupling.py` is the driver and a modification of the original `schnetpack_qm7.py` found in schnetpack to allow for scalar coupling prediction.



