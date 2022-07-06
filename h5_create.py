import h5py
f = h5py.File('my_hdf5_file.h5', 'w')
dset = f.create_dataset("lidar", (2, 2))
dset2 = f.create_dataset("rgb", (100, 2), dtype='f')
dset[0,1] = 3.0  # No effect!
dset.attrs['temperature'] = 99.5
print(dset[0][1])
for name in f:
    print(name)
