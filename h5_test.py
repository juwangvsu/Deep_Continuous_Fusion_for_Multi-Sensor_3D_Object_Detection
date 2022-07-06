import os
wd=os.chdir('.') #change the file path to your working directory
wd=os.getcwd() #request what is the current working directory
print(wd) #show what is the current working directory
if __name__ == '__main__':
	# import required libraries
	import h5py as h5
	import numpy as np
	import matplotlib.pyplot as plt

	# Read H5 file
	#f = h5.File("iris.hdf5", "r")
	f = h5.File("NEONDSImagingSpectrometerData.h5", "r")
	# Get and print list of datasets within the H5 file
	datasetNames = [n for n in f.keys()]
	print('keys: ')
	for n in datasetNames:
		print(n)
		# extract reflectance data from the H5 file
	reflectance = f['Reflectance']
	#reflectance = f['iris']
	print('attrs of a key:')
	for n in reflectance.attrs:
            print(n)
	# extract one pixel from the data
	reflectanceData = reflectance[:,49,392]
	reflectanceData = reflectanceData.astype(float)

	# divide the data by the scale factor to convert the integer values into floating point values
	# note: this information would be accessed from the metadata
	scaleFactor = 10000.0
	reflectanceData /= scaleFactor
	wavelength = f['wavelength']
	wavelengthData = wavelength[:]
	#transpose the data so wavelength values are in one column
	wavelengthData = np.reshape(wavelengthData, 426)
	# Print the attributes (metadata):
	print("Data Description : ", reflectance.attrs['Description'])
	print("Data dimensions : ", reflectance.shape, reflectance.attrs['DIMENSION_LABELS'])
	plt.plot(wavelengthData, reflectanceData)
	plt.title("Vegetation Spectra")
	plt.ylabel('Reflectance')
	plt.ylim((0,1))
	plt.xlabel('Wavelength [$\mu m$]')
	plt.show()
	f.close()
